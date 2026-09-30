/* ============================================================================
 * 抖音「关注 / 粉丝」列表采集器 —— DevTools Console 版
 * ----------------------------------------------------------------------------
 * 原理：不动签名。这是你自己已登录的浏览器，页面本身就会带着合法签名去请求
 *      关注/粉丝列表接口。本脚本只是把页面「已经收到」的响应对下来并归档，
 *      不构造请求、不伪造 a_bogus / x-secsdk-web-signature。
 *
 * 用法：
 *   1. 浏览器（Chrome/Edge）登录 www.douyin.com
 *   2. 打开自己的主页：https://www.douyin.com/user/self?showTab=following
 *   3. F12 → Console → 粘贴本文件全部内容 → 回车
 *   4. 脚本会自动滚动翻页；若没自动翻，手动往下滚，脚本一样会记录
 *   5. 采完后切到「粉丝」标签，重复步骤 4
 *   6. 点右下角【下载 JSON】，把文件放进项目的 data/raw/ 目录
 *
 * 控制台命令：
 *   __dyStatus()   查看当前采集到多少
 *   __dyDump()     手动触发下载
 *   __dyStop()     停止自动滚动
 *   __dyReset()    清空已采集数据（会同时清 localStorage）
 * ========================================================================== */
(() => {
  'use strict';

  if (window.__dyCollector) {
    console.warn('[采集器] 已在运行中，无需重复粘贴。用 __dyStatus() 查看进度。');
    return;
  }

  const STORE_KEY = '__dy_collector_data';
  // 改动去重逻辑等会影响存量数据正确性的地方时，把这个版本号 +1。
  // 旧版本存下来的数据会在加载时被自动丢弃，用户不需要手动清理。
  const STORE_VERSION = 8;

  const state = {
    following: new Map(), // sec_uid -> user
    fans: new Map(),
    autoScroll: true,
    idleRounds: 0,
    scrollTimer: null,
    saveTimer: null,
    // 记录接口自己报的分页状态，用来判断「到底了没有」
    meta: {
      following: { hasMore: null, total: null, pages: 0 },
      fans: { hasMore: null, total: null, pages: 0 },
    },
  };

  /* ------------------------------------------------------------------ 工具 */

  const num = (v) => {
    const n = Number(v);
    return Number.isFinite(n) ? n : 0;
  };

  /** 短哈希：给没有 sec_uid/uid 的记录生成稳定唯一键，避免重名互相覆盖 */
  function hash(str) {
    let h = 5381;
    for (let i = 0; i < str.length; i++) h = ((h << 5) + h + str.charCodeAt(i)) | 0;
    return 'h' + (h >>> 0).toString(36);
  }

  const today = () => {
    const d = new Date();
    const p = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  };

  /** 把接口返回的用户对象拍平成统一结构（与 Python 端 models.parse_user 对齐） */
  function normalize(item) {
    if (!item || typeof item !== 'object') return null;
    const u = item.user || item.user_info || item;
    const stats = item.stats || u.stats || {};

    const secUid = String(u.sec_uid || u.sec_user_id || '');
    const uid = String(u.uid || u.user_id || '');
    const nickname = u.nickname || u.nick_name || '';
    // 绝不用昵称当去重键：抖音重名极多，会把不同的人合并成一条、凭空少人。
    // 没有 ID 时退化为「整条记录的哈希」——同样的记录才会被判定为重复。
    const key = secUid || uid || hash(JSON.stringify(item));
    if (!key) return null;

    const uniqueId = u.unique_id || u.douyin_id || '';
    const shortId = u.short_id || '';

    // 保留接口返回的所有「标量」字段。关注/互关时间戳就藏在这里面，
    // 之前为了瘦身把它们裁掉了，导致拿不到「互关时间」。
    // 只丢嵌套对象/数组，文件不会膨胀太多。
    const KNOWN = new Set([
      'uid', 'sec_uid', 'sec_user_id', 'nickname', 'nick_name', 'unique_id',
      'douyin_id', 'short_id', 'signature', 'desc', 'remark_name', 'remark',
      'follower_count', 'following_count', 'aweme_count', 'custom_verify',
      'follow_status', 'user', 'user_info', 'stats',
    ]);
    const extra = {};
    for (const src of [item, u]) {
      for (const [k, v] of Object.entries(src)) {
        if (KNOWN.has(k) || k in extra) continue;
        if (v === null || typeof v === 'object') continue;
        extra[k] = v;
      }
    }

    return {
      uid,
      sec_uid: secUid,
      nickname,
      unique_id: String(uniqueId || shortId || ''),
      short_id: String(shortId || ''),
      signature: u.signature || u.desc || '',
      remark_name: u.remark_name || u.remark || item.remark_name || '',
      follower_count: num(stats.follower_count ?? u.follower_count),
      following_count: num(stats.following_count ?? u.following_count),
      aweme_count: num(stats.aweme_count ?? u.aweme_count),
      custom_verify: u.custom_verify || '',
      follow_status: num(item.follow_status ?? u.follow_status),
      extra,
      _key: key,
    };
  }

  function ingest(url, text) {
    let type = null;
    if (/\/user\/following\/list\//.test(url)) type = 'following';
    else if (/\/user\/follower\/list\//.test(url)) type = 'fans';
    if (!type) return;

    let json;
    try {
      json = JSON.parse(text);
    } catch (e) {
      return;
    }
    if (!json || typeof json !== 'object') return;

    // 列表字段名各个版本不一样，宽松一点找，别因为换了个键就整页丢掉
    const CANDIDATES = ['followings', 'followers', 'user_list', 'data', 'list', 'items'];
    const arrKey = CANDIDATES.find((k) => Array.isArray(json[k]));
    const list = arrKey ? json[arrKey] : [];

    // 先记分页状态 —— 粉丝列表的首页可能是空的但带着 total/max_time
    const m = state.meta[type];
    // 去重前的原始条数：和去重后的差多少，就知道去重吃掉了多少
    m.rawCount = (m.rawCount || 0) + list.length;
    if (typeof json.has_more !== 'undefined') {
      m.hasMore = json.has_more === true || Number(json.has_more) === 1;
    }
    if (typeof json.total !== 'undefined' && Number(json.total)) {
      m.total = Number(json.total);
    }
    m.pages += 1;

    // 判断列表是按「最近关注」还是「最早关注」返回的。
    // 依据：翻页游标就是关注时间；如果拿到最后一批数据那页的游标
    // 比第一页小，说明是「最近的在前」。用户在页面切换排序会改变它。
    if (list.length && m.firstCursor == null) m.firstCursor = json.max_time;
    if (m.hasMore === false && m.endCursor == null) m.endCursor = json.max_time;

    // 判断列表按「最早关注」还是「最近关注」排：看前两个不同的页面游标。
    // 游标递增 => 从最早翻到最新（最早关注）；递减 => 最近关注。
    // 注意不能用 has_more=false 判断方向 —— 用户从中间往上翻时，
    // 顶部也会报 has_more=false，那是列表开头不是末尾。
    m.pageSeq = m.pageSeq || [];
    if (json.max_time && m.pageSeq.length < 2 && !m.pageSeq.includes(json.max_time)) {
      m.pageSeq.push(json.max_time);
      if (m.pageSeq.length === 2) m.ascending = m.pageSeq[1] > m.pageSeq[0];
    }

    // 这一页是第几页（只数有数据的页）。**要在写日志之前递增**，
    // 否则日志里的 page 会跟用户记录上的 _p 差一位。
    if (list.length) m.pageNo = (m.pageNo || 0) + 1;

    // 逐页留痕。下载的 JSON 里带着这份轨迹，卡住时能直接看出到底是
    // 「页数没拉完」还是「同一个游标被反复返回同一页」。
    m.log = m.log || [];
    if (m.log.length < 400) {
      // 记下接口返回的**全部顶层标量字段**。抖音会改字段名 —— 曾经
      // max_time/min_time 有真实时间戳，后来变成恒为 0，翻页游标换了地方。
      // 全量记下来，才不用靠猜。
      const fields = {};
      for (const [k, v] of Object.entries(json)) {
        if (v === null || typeof v === 'object') continue;
        fields[k] = v;
      }
      m.log.push({
        n: list.length,
        key: arrKey || null,
        more: json.has_more,
        total: json.total,
        max_time: json.max_time,
        min_time: json.min_time,
        page: list.length ? m.pageNo : null,
        fields,
      });
    }
    if (!arrKey) m.unknownShape = (m.unknownShape || 0) + 1;

    scheduleSave();
    render();

    if (!list.length) return;

    const bucket = state[type];
    let added = 0;
    const pageKeys = [];   // 这一页的用户，保持接口返回的先后
    for (let idx = 0; idx < list.length; idx++) {
      const item = list[idx];
      const u = normalize(item);
      if (!u) continue;

      // 记录这条是在「第几页、页内第几个」首次出现的。只在首次见到时写，
      // 避免同一页被重复拉取时覆盖。这是游标失效后唯一可靠的顺序线索。
      const old = bucket.get(u._key);
      if (old) {
        u._p = old._p;
        u._pi = old._pi;
        u.cursor_max = old.cursor_max;
        u.cursor_min = old.cursor_min;
      } else {
        u._p = m.pageNo;
        u._pi = idx;
        if (json.max_time) {
          u.cursor_max = json.max_time;
          u.cursor_min = json.min_time;
        }
      }

      if (!old) added++;
      else {
        // 合并：保留已有非空字段，用新值补空缺
        for (const k of Object.keys(u)) {
          if (!u[k] && old[k]) u[k] = old[k];
        }
      }
      pageKeys.push(u._key);
      bucket.set(u._key, u);
    }

    // 记下每一页的成员和页内先后。接口不给游标时，只要其中有一遍是
    // 「从列表一头翻到另一头」的完整翻页，就能按它把全局顺序拼回来。
    m.pageKeys = m.pageKeys || [];
    if (m.pageKeys.length < 400) m.pageKeys.push(pageKeys);

    if (added > 0) state.idleRounds = 0;
    scheduleSave();
    render();
  }

  /* ------------------------------------------------------------ 网络拦截 */

  const origOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    try {
      this.__dy_url = String(url || '');
    } catch (e) {}
    return origOpen.call(this, method, url, ...rest);
  };

  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function (...args) {
    try {
      const url = this.__dy_url || '';
      if (url) {
        this.addEventListener('load', () => {
          try {
            const rt = this.responseType;
            if (rt && rt !== 'text' && rt !== 'json') return; // blob/arraybuffer 跳过
            let text = this.responseText;
            if (rt === 'json' && typeof this.response === 'object') {
              text = JSON.stringify(this.response);
            }
            if (text) ingest(url, text);
          } catch (e) {}
        });
      }
    } catch (e) {}
    return origSend.apply(this, args);
  };

  const origFetch = window.fetch;
  window.fetch = function (...args) {
    const p = origFetch.apply(this, args);
    try {
      const url = typeof args[0] === 'string' ? args[0] : args[0] && args[0].url;
      if (url && /\/(following|follower)\/list\//.test(String(url))) {
        p.then((res) => {
          res.clone().text().then((t) => ingest(String(url), t)).catch(() => {});
        }).catch(() => {});
      }
    } catch (e) {}
    return p;
  };

  /* ------------------------------------------------------------- 自动滚动 */

  function scrollables() {
    const out = [];
    const all = document.querySelectorAll('div, ul, main, section');
    for (const el of all) {
      if (el.scrollHeight > el.clientHeight + 80 && el.clientHeight > 120) {
        const oy = getComputedStyle(el).overflowY;
        if (oy === 'auto' || oy === 'scroll') out.push(el);
      }
    }
    return out;
  }

  function tick() {
    if (!state.autoScroll) return;

    const before =
      state.following.size + state.fans.size;

    for (const el of scrollables()) {
      el.scrollTop = el.scrollHeight;
    }
    window.scrollTo(0, document.body.scrollHeight);

    setTimeout(() => {
      const after = state.following.size + state.fans.size;
      state.idleRounds = after > before ? 0 : state.idleRounds + 1;
      render();

      // 接口说还有更多时给更长的耐心，避免在长列表上停早
      const pendingMore =
        state.meta.following.hasMore === true || state.meta.fans.hasMore === true;
      const limit = pendingMore ? 25 : 6;

      if (state.idleRounds >= limit) {
        const why = pendingMore
          ? `接口仍报告 has_more=1，但已连续 ${limit} 轮没有新数据 —— 可能是页面卡住，请手动往下滚`
          : '接口报告已到列表末尾';
        state.autoScroll = false;
        log(`已停止自动滚动：${why}`);
        render();
      }
    }, 900);
  }

  function startAutoScroll() {
    if (state.scrollTimer) clearInterval(state.scrollTimer);
    state.autoScroll = true;
    state.idleRounds = 0;
    state.scrollTimer = setInterval(tick, 1200);
  }

  /* --------------------------------------------------------------- 持久化 */

  function dump() {
    // 这里**故意不重排数组**：采集顺序 + 每条的页游标 + m.ascending
    // 已经足够让 Python 还原出正确的时间顺序。在采集器里猜方向只会
    // 帮倒忙（曾经因为误判把每页内部顺序整体翻转）。
    return {
      v: STORE_VERSION,
      collected_at: new Date().toISOString(),
      date: today(),
      source: 'douyin.com (browser session)',
      // 接口自报的分页状态，用来判断这次采集是否完整
      meta: {
        following: { ...state.meta.following },
        fans: { ...state.meta.fans },
      },
      following_count: state.following.size,
      fans_count: state.fans.size,
      following: [...state.following.values()],
      fans: [...state.fans.values()],
    };
  }

  function scheduleSave() {
    if (state.saveTimer) return;
    state.saveTimer = setTimeout(() => {
      state.saveTimer = null;
      try {
        localStorage.setItem(STORE_KEY, JSON.stringify(dump()));
      } catch (e) {
        /* 数据太大时 localStorage 会满，忽略即可，手动下载才是主要出口 */
      }
    }, 2000);
  }

  function restore() {
    try {
      const raw = localStorage.getItem(STORE_KEY);
      if (!raw) return;
      const d = JSON.parse(raw);
      if (d.date !== today()) return; // 只恢复当天的，避免陈旧数据混入
      if (d.v !== STORE_VERSION) {
        // 旧版本的数据（比如去重逻辑有 bug 的那版）直接用不了，丢掉重采
        localStorage.removeItem(STORE_KEY);
        log('检测到旧版本缓存，已自动丢弃，请重新滚动采集。');
        return;
      }
      for (const u of d.following || []) if (u._key) state.following.set(u._key, u);
      for (const u of d.fans || []) if (u._key) state.fans.set(u._key, u);
      for (const k of ['following', 'fans']) {
        if (d.meta && d.meta[k]) Object.assign(state.meta[k], d.meta[k]);
      }
      log(`已从 localStorage 恢复：关注 ${state.following.size} / 粉丝 ${state.fans.size}`);
    } catch (e) {}
  }

  /* -------------------------------------------------------------- 下载 */

  function download() {
    const data = dump();
    if (!data.following.length && !data.fans.length) {
      alert('还没采集到任何数据。请确认：\n1) 停留在我自己的主页\n2) 停在「关注」或「粉丝」标签\n3) 页面列表已经加载出来');
      return;
    }
    const name = state.following.size
      ? `douyin_following_${data.date}.json`
      : `douyin_followers_${data.date}.json`;

    // 两类都有时打成一个合并包；只有一类时按类型命名，方便区分
    const blob = new Blob([JSON.stringify(data, null, 2)], {
      type: 'application/json;charset=utf-8',
    });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = name;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    log(`已下载 ${name}`);
  }

  /* ---------------------------------------------------------------- UI */

  let panel, bodyEl, logEl;

  function buildPanel() {
    panel = document.createElement('div');
    panel.style.cssText = [
      'position:fixed', 'right:16px', 'bottom:16px', 'z-index:2147483647',
      'width:300px', 'background:#1f2430', 'color:#e6e6e6',
      'font:12px/1.6 -apple-system,Segoe UI,Microsoft YaHei,sans-serif',
      'border-radius:10px', 'padding:12px 14px',
      'box-shadow:0 6px 24px rgba(0,0,0,.45)', 'user-select:none',
    ].join(';');

    panel.innerHTML = `
      <div style="font-weight:600;font-size:13px;margin-bottom:6px">抖音列表采集器</div>
      <div data-body></div>
      <div style="display:flex;gap:8px;margin-top:10px">
        <button data-dump style="flex:1;padding:6px;border:0;border-radius:6px;background:#3b82f6;color:#fff;cursor:pointer">下载 JSON</button>
        <button data-toggle style="flex:1;padding:6px;border:0;border-radius:6px;background:#4b5563;color:#fff;cursor:pointer">暂停滚动</button>
      </div>
      <div data-log style="margin-top:8px;color:#9ca3af;font-size:11px;max-height:60px;overflow:auto"></div>
    `;
    document.body.appendChild(panel);

    bodyEl = panel.querySelector('[data-body]');
    logEl = panel.querySelector('[data-log]');

    panel.querySelector('[data-dump]').onclick = download;
    panel.querySelector('[data-toggle]').onclick = (e) => {
      state.autoScroll = !state.autoScroll;
      e.target.textContent = state.autoScroll ? '暂停滚动' : '继续滚动';
      if (state.autoScroll) startAutoScroll();
      render();
    };
  }

  function log(msg) {
    console.log('[采集器]', msg);
    if (!logEl) return;
    const line = document.createElement('div');
    line.textContent = msg;
    logEl.appendChild(line);
    logEl.scrollTop = logEl.scrollHeight;
  }

  function render() {
    if (!bodyEl) return;
    const mutual = [...state.following.keys()].filter((k) => state.fans.has(k)).length;

    const tag = (m) => {
      if (m.hasMore === true) return '<span style="color:#f59e0b">还有更多</span>';
      if (m.hasMore === false) return '<span style="color:#4ade80">已到底</span>';
      return '<span style="color:#9ca3af">?未知</span>';
    };
    const line = (label, count, m, color) => `
      <div>${label}：<b style="color:${color}">${count}</b> 人${
        m.total ? `<span style="color:#9ca3af"> / 接口报 ${m.total}</span>` : ''
      }</div>
      <div style="font-size:11px;margin:0 0 2px 8px">${tag(m)} · 已拉 ${m.pages} 页${
        m.rawCount ? ` · 原始 ${m.rawCount} 条` : ''
      }</div>`;

    bodyEl.innerHTML = `
      ${line('关注', state.following.size, state.meta.following, '#60a5fa')}
      ${line('粉丝', state.fans.size, state.meta.fans, '#4ade80')}
      <div>互关：<b style="color:#fbbf24">${mutual}</b> 人</div>
      <div style="color:#9ca3af;font-size:11px;margin-top:4px">
        ${state.autoScroll ? '自动滚动中…' : '已暂停'} · 空转 ${state.idleRounds}
      </div>`;
  }

  /* ------------------------------------------------------------- 启动 */

  buildPanel();
  restore();
  render();
  startAutoScroll();
  log('已挂载。请停在「关注」或「粉丝」标签，列表加载后会自动记录。');

  window.__dyCollector = true;
  window.__dyStatus = () => dump();
  window.__dyDump = download;
  window.__dyStop = () => {
    state.autoScroll = false;
    if (state.scrollTimer) clearInterval(state.scrollTimer);
    render();
    log('已停止。');
  };
  window.__dyReset = () => {
    state.following.clear();
    state.fans.clear();
    try { localStorage.removeItem(STORE_KEY); } catch (e) {}
    render();
    log('已清空。');
  };
})();
