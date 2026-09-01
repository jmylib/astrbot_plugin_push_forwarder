/* 推送转发面板。
 *
 * 运行在 Dashboard 的受限 iframe 中：没有 allow-modals，因此不能用
 * alert / confirm / prompt，所有反馈走页面内的 toast；也不能引用外部资源。
 * 所有来自平台或用户的文本一律用 textContent 写入，避免群名里的 HTML 被解析。
 */

(function () {
  'use strict';

  /* bridge SDK（/api/plugin/page/bridge-sdk.js）是 AstrBot 注入到本页的，
     注入位置和加载时机都不由我们控制，所以每次用的时候现取，别在脚本顶部缓存。 */
  function getBridge() {
    return window.AstrBotPluginPage || null;
  }

  /* 轮询等待 bridge 就绪。SDK 若以 module/async 方式注入，会晚于本脚本执行。 */
  function waitForBridge(timeoutMs) {
    var deadline = Date.now() + (timeoutMs || 8000);
    return new Promise(function (resolve, reject) {
      (function poll() {
        var b = getBridge();
        if (b && typeof b.apiGet === 'function') return resolve(b);
        if (Date.now() > deadline) {
          return reject(
            new Error(
              '页面没有拿到 AstrBot 的 bridge SDK。请确认这个页面是从 WebUI 的插件面板入口打开的，' +
                '并且 AstrBot 版本支持插件 Pages（4.17 及以上）。'
            )
          );
        }
        setTimeout(poll, 50);
      })();
    });
  }

  var GROUP = 'GroupMessage';
  var FRIEND = 'FriendMessage';
  var DAY_NAMES = ['一', '二', '三', '四', '五', '六', '日'];
  var HOST_KEY = 'pf_webhook_host';

  var state = {
    webhook: null,
    bots: [],
    currentBotId: null,
    sessions: [],
    targets: [],
    sessionType: GROUP,
    keyword: '',
    onlyCurrentBot: true,
    dirty: false,
    tokenVisible: false,
    hostOverride: '',
    scheduleCtx: null // { kind: 'bot' | 'target', botId, targetKey, schedule }
  };

  var keySeq = 0;

  // ------------------------------------------------------------------ 工具

  function $(id) {
    return document.getElementById(id);
  }

  function el(tag, props, children) {
    var node = document.createElement(tag);
    props = props || {};
    Object.keys(props).forEach(function (key) {
      if (key === 'class') node.className = props[key];
      else if (key === 'text') node.textContent = props[key];
      else if (key === 'html') node.innerHTML = props[key]; // 仅用于本文件内的静态字符串
      else if (key.indexOf('on') === 0) node.addEventListener(key.slice(2), props[key]);
      else if (props[key] === true) node.setAttribute(key, '');
      else if (props[key] !== false && props[key] != null) node.setAttribute(key, props[key]);
    });
    (children || []).forEach(function (child) {
      if (child) node.appendChild(child);
    });
    return node;
  }

  var toastTimer = null;
  function toast(message, isError) {
    var box = $('toast');
    box.textContent = message;
    box.className = 'toast' + (isError ? ' error' : '');
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      box.className = 'toast hidden';
    }, 2600);
  }

  function copyText(text, label) {
    function fallback() {
      var area = document.createElement('textarea');
      area.value = text;
      area.style.position = 'fixed';
      area.style.opacity = '0';
      document.body.appendChild(area);
      area.select();
      try {
        document.execCommand('copy');
        toast(label + ' 已复制');
      } catch (e) {
        toast('复制失败，请手动选中复制', true);
      }
      document.body.removeChild(area);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () {
        toast(label + ' 已复制');
      }, fallback);
    } else {
      fallback();
    }
  }

  function formatTime(ts) {
    if (!ts) return '从未';
    var d = new Date(ts * 1000);
    var now = Date.now();
    var diff = (now - d.getTime()) / 1000;
    if (diff < 60) return '刚刚';
    if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
    if (diff < 86400) return Math.floor(diff / 3600) + ' 小时前';
    if (diff < 86400 * 30) return Math.floor(diff / 86400) + ' 天前';
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  }

  function pad(n) {
    return n < 10 ? '0' + n : String(n);
  }

  // ------------------------------------------------------------------ 后端调用

  function unwrap(res) {
    // 后端出错时返回 {status:'error', message}，HTTP 仍是 200，bridge 不会 reject
    if (res && res.status === 'error') {
      throw new Error(res.message || '操作失败');
    }
    return res;
  }

  /* 把后端 4xx/5xx 之类的裸错误补上接口名，否则面板上只剩一句 "请求失败"。 */
  function describeError(path, e) {
    var msg = (e && e.message) || String(e || '请求失败');
    return '调用接口 ' + path + ' 失败：' + msg;
  }

  function apiGet(path, params) {
    var bridge = getBridge();
    if (!bridge) return Promise.reject(new Error('未在 AstrBot 面板中打开'));
    return bridge.apiGet(path, params || {}).then(unwrap, function (e) {
      throw new Error(describeError(path, e));
    });
  }

  function apiPost(path, body) {
    var bridge = getBridge();
    if (!bridge) return Promise.reject(new Error('未在 AstrBot 面板中打开'));
    return bridge.apiPost(path, body || {}).then(unwrap, function (e) {
      throw new Error(describeError(path, e));
    });
  }

  function fail(e) {
    toast((e && e.message) || '请求失败', true);
  }

  // ------------------------------------------------------------------ 顶部条

  /* 生成地址用的主机名。
   * 监听在 0.0.0.0 时服务端并不知道推送方该用哪个 IP，默认取 WebUI 当前地址，
   * 但用户从内网打开 WebUI、推送方却在外网时这个值是错的，所以允许手动覆盖。 */
  function webhookHost() {
    var manual = (state.hostOverride || '').trim();
    if (manual) return manual;
    var info = state.webhook;
    if (info && info.host && info.host !== '0.0.0.0' && info.host !== '::') return info.host;
    return location.hostname || '你的服务器IP';
  }

  function baseUrl() {
    var info = state.webhook;
    if (!info) return '';
    return 'http://' + webhookHost() + ':' + info.port + info.path;
  }

  /* 带 Token 的完整地址：很多推送工具只给填一个 URL，没法加请求头。 */
  function fullUrl(masked) {
    var info = state.webhook;
    if (!info) return '';
    var token = info.token || '';
    if (!token) return baseUrl();
    // 打码只是给人看的，别转义成 %E2%80%A2 一串
    return baseUrl() + '?token=' + (masked ? maskToken(token) : encodeURIComponent(token));
  }

  function renderWebhook() {
    var info = state.webhook;
    if (!info) return;

    $('webhook-url').textContent = fullUrl(!state.tokenVisible);
    $('webhook-token').textContent = state.tokenVisible ? info.token : maskToken(info.token);
    $('toggle-token').textContent = state.tokenVisible ? '隐藏' : '显示';
    $('stat-targets').textContent = state.targets.length;
    $('stat-queued').textContent = info.queued || 0;

    var dot = $('service-dot');
    dot.className = 'dot ' + (info.running ? 'on' : 'off');
    dot.title = info.running
      ? '接收服务运行中' + (info.bound ? '，实际监听 ' + info.bound : '')
      : '接收服务未运行';

    /* 上面那行「推送地址」的主机名取自浏览器地址栏，是给推送方填的；
       这里是服务在 AstrBot 进程里真正 bind 到的地址。Docker 部署时两者经常
       对不上（容器内开着、宿主机没映射），分开显示才看得出问题出在哪。 */
    $('listen-addr').textContent =
      (info.bound || (info.host || '0.0.0.0') + ':' + info.port) + (info.path || '');

    var pill = $('listen-state');
    if (!info.enabled) {
      pill.textContent = '已关闭';
      pill.className = 'pill';
    } else if (info.running) {
      pill.textContent = '运行中';
      pill.className = 'pill ok';
    } else {
      pill.textContent = '未运行';
      pill.className = 'pill bad';
    }
    $('listen-note').textContent = info.running
      ? '容器内的监听地址，Docker 需把 ' + info.port + ' 端口映射到宿主机'
      : '';

    var warn = $('service-warning');
    if (!info.enabled) {
      warn.textContent = '推送接收服务已在插件配置中关闭，当前只能通过面板手动发送测试消息。';
      warn.className = 'banner banner-warn';
    } else if (!info.running) {
      warn.textContent = '推送接收服务未运行：' + (info.last_error || '原因未知')
        + '。端口多半被别的程序占用了，换个端口后重载插件；也可以在聊天里发 /fwd start 就地重试。';
      warn.className = 'banner banner-warn';
    } else {
      warn.className = 'banner banner-warn hidden';
    }
  }

  function maskToken(token) {
    if (!token) return '（未设置）';
    if (token.length <= 8) return '••••••••';
    return token.slice(0, 4) + '••••••••' + token.slice(-4);
  }

  function curlSample() {
    var info = state.webhook;
    return (
      'curl -X POST ' + baseUrl() +
      ' \\\n  -H "X-Token: ' + info.token + '"' +
      ' \\\n  -H "Content-Type: application/json"' +
      ' \\\n  -d \'{"title":"服务器告警","text":"CPU 使用率 95%","tags":["alert"]}\''
    );
  }

  // ------------------------------------------------------------------ 机器人栏

  function currentBot() {
    for (var i = 0; i < state.bots.length; i++) {
      if (state.bots[i].platform_id === state.currentBotId) return state.bots[i];
    }
    return null;
  }

  function renderBots() {
    var box = $('bot-list');
    box.textContent = '';

    if (!state.bots.length) {
      box.appendChild(
        el('div', { class: 'empty', text: '没有找到任何机器人实例。请先在 AstrBot 的「消息平台」中添加并启用一个机器人。' })
      );
      return;
    }

    state.bots.forEach(function (bot) {
      var caps = bot.caps || {};
      var limited = bot.schedule && bot.schedule.enabled;

      var badges = el('div', { class: 'badges' }, [
        el('span', { class: 'badge ' + (caps.group ? 'on' : 'off'), text: '群聊' }),
        el('span', { class: 'badge ' + (caps.private ? 'on' : 'off'), text: '私聊' }),
        el('span', { class: 'badge ' + (caps.at ? 'on' : 'off'), text: '@提醒' }),
        bot.enabled ? null : el('span', { class: 'badge off', text: '已停用' })
      ]);

      var node = el(
        'div',
        {
          class: 'bot' + (bot.platform_id === state.currentBotId ? ' active' : ''),
          title: caps.note || '',
          onclick: function () {
            selectBot(bot.platform_id);
          }
        },
        [
          el('div', { class: 'bot-name', text: bot.remark || bot.platform_id }),
          el('div', { class: 'bot-type', text: bot.platform_name + ' · ' + bot.target_count + ' 个目标' }),
          badges,
          el('div', { class: 'bot-foot' }, [
            el('span', {
              class: 'schedule-chip' + (limited ? ' limited' : ''),
              text: bot.schedule_text || '全天不限',
              title: bot.schedule_text || '全天不限'
            }),
            el('button', {
              class: 'btn btn-ghost btn-sm',
              text: '时段',
              onclick: function (ev) {
                ev.stopPropagation();
                openScheduleModal('bot', bot);
              }
            })
          ])
        ]
      );
      box.appendChild(node);
    });
  }

  function selectBot(platformId) {
    state.currentBotId = platformId;
    var bot = currentBot();
    var caps = (bot && bot.caps) || {};

    // 平台不支持的会话类型直接禁用 Tab，避免配出永远不生效的目标
    var tabs = $('session-tabs').querySelectorAll('.tab');
    for (var i = 0; i < tabs.length; i++) {
      var type = tabs[i].getAttribute('data-type');
      var ok = type === GROUP ? caps.group !== false : caps.private !== false;
      tabs[i].disabled = !ok;
      tabs[i].title = ok ? '' : (caps.note || '该平台不支持此类会话');
    }
    if (state.sessionType === GROUP && caps.group === false) state.sessionType = FRIEND;
    if (state.sessionType === FRIEND && caps.private === false) state.sessionType = GROUP;

    $('refresh-sessions').className = caps.listable ? 'btn btn-ghost btn-sm' : 'btn btn-ghost btn-sm hidden';

    renderBots();
    renderTabs();
    renderTargets();
    loadSessions(true);
  }

  // -------------------------------------------------------------------- 会话栏

  function renderTabs() {
    var tabs = $('session-tabs').querySelectorAll('.tab');
    for (var i = 0; i < tabs.length; i++) {
      var active = tabs[i].getAttribute('data-type') === state.sessionType;
      tabs[i].className = 'tab' + (active ? ' active' : '');
    }
  }

  var sessionReq = 0;

  function loadSessions(showLoading) {
    if (!state.currentBotId) return Promise.resolve();

    // 切换机器人或标签页时先清空，否则会短暂显示上一个机器人的会话
    if (showLoading) {
      var box = $('session-list');
      box.textContent = '';
      box.appendChild(el('div', { class: 'empty', text: '加载中…' }));
    }

    var seq = ++sessionReq;
    return apiGet('sessions', {
      platform_id: state.currentBotId,
      type: state.sessionType,
      q: state.keyword
    })
      .then(function (data) {
        if (seq !== sessionReq) return; // 快速切换时丢弃过期响应，避免覆盖新数据
        state.sessions = (data && data.sessions) || [];
        renderSessions(data && data.discovery_enabled);
      })
      .catch(function (e) {
        if (seq === sessionReq) fail(e);
      });
  }

  function renderSessions(discoveryEnabled) {
    var box = $('session-list');
    box.textContent = '';

    var bot = currentBot();
    var caps = (bot && bot.caps) || {};
    var typeName = state.sessionType === GROUP ? '群聊' : '私聊';

    if (state.sessionType === GROUP && caps.group === false) {
      box.appendChild(
        el('div', { class: 'empty' }, [
          el('div', { text: caps.note || '该平台适配器不支持群消息。' }),
          el('div', { text: '请切换到「私聊」标签页选择转发目标。' })
        ])
      );
      return;
    }

    if (!state.sessions.length) {
      var tips = el('div', { class: 'empty' }, [
        el('div', { text: '还没有发现任何' + typeName + '会话。' })
      ]);
      if (discoveryEnabled === false) {
        tips.appendChild(el('div', { text: '会话发现已在插件配置中关闭，请先启用它。' }));
      } else if (caps.listable) {
        tips.appendChild(el('div', { text: '可以点击右上角「从平台拉取」，或让机器人先收到一条消息。' }));
      } else {
        tips.appendChild(
          el('div', { text: '该平台无法查询会话列表，需要机器人先在目标会话里收到一条消息。' })
        );
        var line = el('div', {});
        line.appendChild(document.createTextNode('也可以直接在该会话中发送指令 '));
        line.appendChild(el('code', { text: '/fwd here' }));
        line.appendChild(document.createTextNode(' 来添加。'));
        tips.appendChild(line);
      }
      box.appendChild(tips);
      return;
    }

    state.sessions.forEach(function (session) {
      var checked = findTarget(session.umo) !== null;
      var meta = [];
      if (session.short_id) meta.push('ID ' + session.short_id);
      if (session.last_sender_name) meta.push('最近发言 ' + session.last_sender_name);
      meta.push(formatTime(session.last_active));
      if (session.source === 'api') meta.push('来自平台列表');

      var checkbox = el('input', { type: 'checkbox' });
      checkbox.checked = checked;
      checkbox.addEventListener('change', function () {
        toggleSession(session, checkbox.checked);
      });

      box.appendChild(
        el('label', { class: 'session' }, [
          checkbox,
          el('div', { class: 'session-main' }, [
            el('div', { class: 'session-name', text: session.display_name || session.session_id }),
            el('div', { class: 'session-meta', text: meta.join(' · ') })
          ])
        ])
      );
    });
  }

  // -------------------------------------------------------------- 转发目标栏

  function findTarget(umo) {
    for (var i = 0; i < state.targets.length; i++) {
      if (state.targets[i].umo === umo) return state.targets[i];
    }
    return null;
  }

  function toggleSession(session, checked) {
    if (checked) {
      if (findTarget(session.umo)) return;
      state.targets.push({
        _key: 'k' + ++keySeq,
        id: '',
        platform_id: session.platform_id,
        umo: session.umo,
        message_type: session.message_type,
        display_name: session.display_name || session.session_id,
        enabled: true,
        tags: [],
        at_mode: 'none',
        at_users: [],
        schedule_inherit: true,
        schedule: defaultSchedule()
      });
    } else {
      state.targets = state.targets.filter(function (t) {
        return t.umo !== session.umo;
      });
    }
    markDirty();
    renderTargets();
  }

  function visibleTargets() {
    if (!state.onlyCurrentBot) return state.targets;
    return state.targets.filter(function (t) {
      return t.platform_id === state.currentBotId;
    });
  }

  function renderTargets() {
    var box = $('target-list');
    box.textContent = '';

    var list = visibleTargets();
    $('target-count').textContent = '(' + list.length + (state.onlyCurrentBot ? ' / ' + state.targets.length : '') + ')';

    if (!list.length) {
      box.appendChild(
        el('div', {
          class: 'empty',
          text: '还没有选择转发目标。在中间的会话列表里勾选群或私聊即可添加。'
        })
      );
      return;
    }

    list.forEach(function (target) {
      box.appendChild(renderTargetCard(target));
    });
  }

  function renderTargetCard(target) {
    var bot = botOf(target.platform_id);
    var caps = (bot && bot.caps) || {};
    var isGroup = target.message_type === GROUP;
    var atUsable = isGroup && caps.at !== false;
    var unsupported = '';
    if (isGroup && caps.group === false) unsupported = caps.note || '该平台不支持群消息，此目标不会收到推送';
    if (!isGroup && caps.private === false) unsupported = '该平台不支持私聊消息，此目标不会收到推送';

    var enableBox = el('input', { type: 'checkbox', title: '启用' });
    enableBox.checked = target.enabled !== false;
    enableBox.addEventListener('change', function () {
      target.enabled = enableBox.checked;
      markDirty();
      renderTargets();
    });

    var tagInput = el('input', { type: 'text', placeholder: '留空则接收全部推送' });
    tagInput.value = (target.tags || []).join(',');
    tagInput.addEventListener('change', function () {
      target.tags = tagInput.value
        .split(/[,，]/)
        .map(function (s) {
          return s.trim();
        })
        .filter(Boolean);
      markDirty();
    });

    var atSelect = el('select', {});
    [
      ['none', '不 @'],
      ['all', '@全体成员'],
      ['users', '@指定成员']
    ].forEach(function (opt) {
      var o = el('option', { value: opt[0], text: opt[1] });
      if (target.at_mode === opt[0]) o.selected = true;
      atSelect.appendChild(o);
    });
    atSelect.disabled = !atUsable;
    atSelect.title = atUsable ? '' : isGroup ? '该平台不支持 @ 提醒' : '私聊无需 @';
    atSelect.addEventListener('change', function () {
      target.at_mode = atSelect.value;
      markDirty();
      renderTargets();
    });

    var rows = [
      el('label', {}, [el('span', { text: '标签' }), tagInput]),
      el('label', {}, [el('span', { text: '@ 提醒' }), atSelect])
    ];

    if (target.at_mode === 'users' && atUsable) {
      var usersInput = el('input', { type: 'text', placeholder: '成员 ID，逗号分隔' });
      usersInput.value = (target.at_users || []).join(',');
      usersInput.addEventListener('change', function () {
        target.at_users = usersInput.value
          .split(/[,，]/)
          .map(function (s) {
            return s.trim();
          })
          .filter(Boolean);
        markDirty();
      });
      rows.push(el('label', {}, [el('span', { text: '@ 成员' }), usersInput]));
    }

    var inheritBox = el('input', { type: 'checkbox' });
    inheritBox.checked = target.schedule_inherit !== false;
    inheritBox.addEventListener('change', function () {
      target.schedule_inherit = inheritBox.checked;
      markDirty();
      renderTargets();
    });

    var schedText = target.schedule_inherit !== false
      ? '跟随机器人：' + ((bot && bot.schedule_text) || '全天不限')
      : describeSchedule(target.schedule);

    var schedRow = el('div', { class: 'target-sched' }, [
      el('label', { class: 'filter-toggle' }, [inheritBox, el('span', { text: '跟随机器人时段' })]),
      el('span', { class: 'schedule-chip', text: schedText, title: schedText })
    ]);

    if (target.schedule_inherit === false) {
      schedRow.appendChild(
        el('button', {
          class: 'btn btn-ghost btn-sm',
          text: '编辑',
          onclick: function () {
            openScheduleModal('target', target);
          }
        })
      );
    }

    var card = el(
      'div',
      { class: 'target' + (target.enabled === false ? ' disabled' : '') + (unsupported ? ' invalid' : '') },
      [
        el('div', { class: 'target-head' }, [
          enableBox,
          el('div', { class: 'target-name', text: target.display_name || target.umo }),
          el('span', { class: 'badge', text: isGroup ? '群' : '私' }),
          el('button', {
            class: 'btn btn-danger btn-sm',
            text: '移除',
            title: '移除后点击保存才会生效',
            onclick: function () {
              state.targets = state.targets.filter(function (t) {
                return t !== target;
              });
              markDirty();
              renderTargets();
              renderSessions(true);
            }
          })
        ]),
        el('div', { class: 'target-rows' }, rows),
        schedRow
      ]
    );

    if (unsupported) {
      card.appendChild(el('div', { class: 'target-note', text: unsupported }));
    }
    if (!state.onlyCurrentBot) {
      card.insertBefore(
        el('div', { class: 'session-meta', text: (bot && bot.remark) || target.platform_id }),
        card.firstChild
      );
    }
    return card;
  }

  function botOf(platformId) {
    for (var i = 0; i < state.bots.length; i++) {
      if (state.bots[i].platform_id === platformId) return state.bots[i];
    }
    return null;
  }

  function markDirty() {
    state.dirty = true;
    $('save-targets').disabled = false;
    $('save-hint').textContent = '有未保存的修改';
  }

  function saveTargets() {
    var payload = state.targets.map(function (t) {
      return {
        id: t.id || '',
        platform_id: t.platform_id,
        umo: t.umo,
        message_type: t.message_type,
        display_name: t.display_name,
        enabled: t.enabled !== false,
        tags: t.tags || [],
        at_mode: t.at_mode || 'none',
        at_users: t.at_users || [],
        schedule_inherit: t.schedule_inherit !== false,
        schedule: t.schedule || defaultSchedule()
      };
    });

    $('save-targets').disabled = true;
    apiPost('targets/save', { targets: payload })
      .then(function (data) {
        state.dirty = false;
        $('save-hint').textContent = '已保存 ' + data.saved + ' 个目标';
        if (data.rejected) {
          toast('有 ' + data.rejected + ' 个未知会话被忽略', true);
        } else {
          toast('已保存');
        }
        return refreshAll();
      })
      .catch(function (e) {
        $('save-targets').disabled = false;
        fail(e);
      });
  }

  function sendTest() {
    var ids = visibleTargets()
      .filter(function (t) {
        return t.enabled !== false && t.id;
      })
      .map(function (t) {
        return t.id;
      });

    if (!ids.length) {
      toast('没有可测试的目标，请先保存', true);
      return;
    }
    if (state.dirty) {
      toast('请先保存修改，再发送测试', true);
      return;
    }

    apiPost('test', { target_ids: ids })
      .then(function (data) {
        var s = data.summary || {};
        toast('测试完成：成功 ' + (s.sent || 0) + '，失败 ' + (s.failed || 0) + '，跳过 ' + (s.skipped || 0));
      })
      .catch(fail);
  }

  // ------------------------------------------------------------------ 时段弹窗

  function defaultSchedule() {
    return {
      enabled: false,
      timezone: 'Asia/Shanghai',
      days: [1, 2, 3, 4, 5, 6, 7],
      ranges: [['09:00', '18:00']],
      outside_action: 'queue'
    };
  }

  function describeSchedule(schedule) {
    if (!schedule || !schedule.enabled) return '全天不限';
    var days = (schedule.days || []).slice().sort();
    var ranges = schedule.ranges || [];
    if (!days.length || !ranges.length) return '全天禁止（未配置有效星期或时间段）';
    var dayText = days.length === 7
      ? '每天'
      : '周' + days.map(function (d) {
          return DAY_NAMES[d - 1];
        }).join('');
    var rangeText = ranges.map(function (r) {
      return r[0] + '-' + r[1];
    }).join('、');
    return dayText + ' ' + rangeText;
  }

  function openScheduleModal(kind, subject) {
    var schedule = kind === 'bot'
      ? JSON.parse(JSON.stringify(subject.schedule || defaultSchedule()))
      : JSON.parse(JSON.stringify(subject.schedule || defaultSchedule()));

    state.scheduleCtx = { kind: kind, subject: subject, schedule: schedule };

    $('schedule-bot-name').textContent = kind === 'bot'
      ? (subject.remark || subject.platform_id)
      : (subject.display_name || subject.umo);
    $('schedule-enabled').checked = !!schedule.enabled;
    $('schedule-tz').value = schedule.timezone || 'Asia/Shanghai';

    var radios = $('schedule-action').querySelectorAll('input');
    for (var i = 0; i < radios.length; i++) {
      radios[i].checked = radios[i].value === (schedule.outside_action || 'queue');
    }

    renderDays();
    renderRanges();
    updatePreview();
    $('schedule-modal').className = 'modal-mask';
  }

  function renderDays() {
    var box = $('schedule-days');
    box.textContent = '';
    var schedule = state.scheduleCtx.schedule;
    DAY_NAMES.forEach(function (name, idx) {
      var day = idx + 1;
      var on = (schedule.days || []).indexOf(day) >= 0;
      box.appendChild(
        el('button', {
          class: 'day' + (on ? ' on' : ''),
          text: name,
          onclick: function () {
            var days = schedule.days || [];
            var pos = days.indexOf(day);
            if (pos >= 0) days.splice(pos, 1);
            else days.push(day);
            days.sort();
            schedule.days = days;
            renderDays();
            updatePreview();
          }
        })
      );
    });
  }

  function renderRanges() {
    var box = $('schedule-ranges');
    box.textContent = '';
    var schedule = state.scheduleCtx.schedule;
    (schedule.ranges || []).forEach(function (range, idx) {
      var start = el('input', { type: 'time', value: range[0] });
      var end = el('input', { type: 'time', value: range[1] });
      start.addEventListener('change', function () {
        range[0] = start.value;
        updatePreview();
      });
      end.addEventListener('change', function () {
        range[1] = end.value;
        updatePreview();
      });
      box.appendChild(
        el('div', { class: 'range-row' }, [
          start,
          el('span', { text: '至' }),
          end,
          el('button', {
            class: 'btn btn-danger btn-sm',
            text: '删除',
            onclick: function () {
              schedule.ranges.splice(idx, 1);
              renderRanges();
              updatePreview();
            }
          })
        ])
      );
    });
  }

  function collectSchedule() {
    var schedule = state.scheduleCtx.schedule;
    schedule.enabled = $('schedule-enabled').checked;
    schedule.timezone = $('schedule-tz').value.trim() || 'Asia/Shanghai';
    var radios = $('schedule-action').querySelectorAll('input');
    for (var i = 0; i < radios.length; i++) {
      if (radios[i].checked) schedule.outside_action = radios[i].value;
    }
    return schedule;
  }

  function updatePreview() {
    var schedule = collectSchedule();
    var text = describeSchedule(schedule);
    if (schedule.enabled && (!schedule.days.length || !schedule.ranges.length)) {
      text += ' —— 这样配置会导致所有消息都不在时段内';
    }
    $('schedule-preview').textContent = '生效规则：' + text;
  }

  function saveSchedule() {
    var ctx = state.scheduleCtx;
    var schedule = collectSchedule();

    if (ctx.kind === 'target') {
      ctx.subject.schedule = schedule;
      closeScheduleModal();
      markDirty();
      renderTargets();
      toast('已更新，点击保存后生效');
      return;
    }

    apiPost('schedule/save', {
      platform_id: ctx.subject.platform_id,
      enabled: ctx.subject.enabled !== false,
      remark: ctx.subject.remark || '',
      schedule: schedule
    })
      .then(function (data) {
        ctx.subject.schedule = data.schedule;
        ctx.subject.schedule_text = data.schedule_text;
        closeScheduleModal();
        renderBots();
        renderTargets();
        toast('转发时段已保存');
      })
      .catch(fail);
  }

  function closeScheduleModal() {
    $('schedule-modal').className = 'modal-mask hidden';
    state.scheduleCtx = null;
  }

  // ------------------------------------------------------------------ 推送记录

  function openHistory() {
    apiGet('history', { limit: 30 })
      .then(function (data) {
        var box = $('history-body');
        box.textContent = '';
        var records = (data && data.records) || [];
        if (!records.length) {
          box.appendChild(el('div', { class: 'empty', text: '还没有推送记录。' }));
        }
        records.forEach(function (rec) {
          var pills = el('div', { class: 'record-results' });
          (rec.results || []).forEach(function (r) {
            pills.appendChild(
              el('span', {
                class: 'pill ' + r.status,
                text: (r.display_name || r.target_id) + ' · ' + statusText(r.status),
                title: r.detail || ''
              })
            );
          });
          box.appendChild(
            el('div', { class: 'record' }, [
              el('div', { class: 'record-head' }, [
                el('strong', { text: rec.title || '(无标题)' }),
                el('span', { class: 'record-time', text: formatTime(rec.ts) }),
                rec.source === 'queue' ? el('span', { class: 'pill queued', text: '补发' }) : null
              ]),
              el('div', { class: 'record-preview', text: rec.preview || '' }),
              pills
            ])
          );
        });
        $('history-modal').className = 'modal-mask';
      })
      .catch(fail);
  }

  function statusText(status) {
    return { sent: '已发送', failed: '失败', queued: '排队', skipped: '跳过' }[status] || status;
  }

  // -------------------------------------------------------------------- 初始化

  function refreshAll() {
    return Promise.all([apiGet('webhook'), apiGet('bots'), apiGet('targets')])
      .then(function (results) {
        state.webhook = results[0];
        state.bots = (results[1] && results[1].bots) || [];
        state.targets = ((results[2] && results[2].targets) || []).map(function (t) {
          t._key = 'k' + ++keySeq;
          return t;
        });

        if (!state.currentBotId || !botOf(state.currentBotId)) {
          state.currentBotId = state.bots.length ? state.bots[0].platform_id : null;
        }

        state.dirty = false;
        $('save-targets').disabled = true;
        $('save-hint').textContent = '';

        renderWebhook();
        if (state.currentBotId) {
          selectBot(state.currentBotId);
        } else {
          renderBots();
          renderTargets();
        }
      });
    // 这里不吞异常：首次加载要冒泡到 showFatal，手动刷新由调用方接住
  }

  function bindEvents() {
    $('copy-full-url').addEventListener('click', function () {
      copyText(fullUrl(false), '完整推送地址');
    });
    $('copy-url').addEventListener('click', function () {
      copyText(baseUrl(), '推送地址');
    });
    $('webhook-host').addEventListener('input', function () {
      state.hostOverride = this.value;
      try {
        localStorage.setItem(HOST_KEY, this.value);
      } catch (e) {
        /* 隐私模式或禁用了站点数据时忽略，只是下次要重填 */
      }
      renderWebhook();
    });
    $('copy-token').addEventListener('click', function () {
      copyText((state.webhook && state.webhook.token) || '', 'Token');
    });
    $('toggle-token').addEventListener('click', function () {
      state.tokenVisible = !state.tokenVisible;
      renderWebhook();
    });
    $('copy-curl').addEventListener('click', function () {
      copyText(curlSample(), 'curl 示例');
    });
    $('reload-bots').addEventListener('click', function () {
      refreshAll()
        .then(function () {
          toast('已刷新');
        })
        .catch(fail);
    });
    $('open-history').addEventListener('click', openHistory);
    $('history-close').addEventListener('click', function () {
      $('history-modal').className = 'modal-mask hidden';
    });

    var tabs = $('session-tabs').querySelectorAll('.tab');
    for (var i = 0; i < tabs.length; i++) {
      (function (tab) {
        tab.addEventListener('click', function () {
          if (tab.disabled) return;
          state.sessionType = tab.getAttribute('data-type');
          renderTabs();
          loadSessions(true);
        });
      })(tabs[i]);
    }

    var searchTimer = null;
    $('session-search').addEventListener('input', function (ev) {
      state.keyword = ev.target.value;
      if (searchTimer) clearTimeout(searchTimer);
      searchTimer = setTimeout(loadSessions, 250);
    });

    $('refresh-sessions').addEventListener('click', function () {
      apiPost('sessions/refresh', { platform_id: state.currentBotId })
        .then(function (data) {
          toast('已拉取 ' + data.count + ' 个会话');
          return loadSessions();
        })
        .catch(fail);
    });

    $('filter-current-bot').addEventListener('change', function (ev) {
      state.onlyCurrentBot = ev.target.checked;
      renderTargets();
    });

    $('save-targets').addEventListener('click', saveTargets);
    $('test-targets').addEventListener('click', sendTest);

    $('schedule-close').addEventListener('click', closeScheduleModal);
    $('schedule-save').addEventListener('click', saveSchedule);
    $('schedule-enabled').addEventListener('change', updatePreview);
    $('schedule-tz').addEventListener('change', updatePreview);
    $('add-range').addEventListener('click', function () {
      state.scheduleCtx.schedule.ranges.push(['09:00', '18:00']);
      renderRanges();
      updatePreview();
    });
    $('schedule-action').addEventListener('change', updatePreview);

    $('schedule-modal').addEventListener('click', function (ev) {
      if (ev.target === $('schedule-modal')) closeScheduleModal();
    });
    $('history-modal').addEventListener('click', function (ev) {
      if (ev.target === $('history-modal')) $('history-modal').className = 'modal-mask hidden';
    });
  }

  function applyTheme(ctx) {
    document.documentElement.setAttribute('data-theme', ctx && ctx.isDark ? 'dark' : 'light');
  }

  /* 加载失败必须留在页面上。用 toast 的话 2.6 秒后就没了，
     用户看到的只剩一个永远"加载中…"的面板，无从下手。 */
  function showFatal(e) {
    var msg = (e && e.message) || String(e || '未知错误');
    var box = $('service-warning');
    box.textContent = '面板加载失败：' + msg;
    box.className = 'banner banner-warn';
    $('webhook-url').textContent = '加载失败';
    $('bot-list').textContent = '';
    $('bot-list').appendChild(
      el('div', { class: 'empty', text: '机器人列表未能加载。修好上面提示的问题后刷新本页。' })
    );
    if (window.console && console.error) console.error('[push_forwarder]', e);
  }

  function init() {
    try {
      state.hostOverride = localStorage.getItem(HOST_KEY) || '';
    } catch (e) {
      state.hostOverride = '';
    }

    try {
      $('webhook-host').value = state.hostOverride;
      bindEvents();
    } catch (e) {
      showFatal(e);
      return;
    }

    waitForBridge(8000)
      .then(function (bridge) {
        var ctx = typeof bridge.ready === 'function' ? bridge.ready() : Promise.resolve(null);
        return Promise.resolve(ctx).then(function (context) {
          applyTheme(context);
          if (typeof bridge.onContext === 'function') {
            bridge.onContext(applyTheme);
          }
          return refreshAll();
        });
      })
      .catch(showFatal);
  }

  init();
})();
