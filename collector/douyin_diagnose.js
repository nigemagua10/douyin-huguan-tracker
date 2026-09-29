/* ============================================================================
 * 抖音关注/粉丝接口 —— 诊断脚本
 * ----------------------------------------------------------------------------
 * 用途：把接口「真实返回结构」打到控制台，用来定位数量对不上的原因。
 *      只打印，不采集、不下载、不改页面。
 *
 * 用法（务必按顺序，钩子必须在请求发生之前装上）：
 *   1. F12 → Console（或 Sources → Snippets）粘贴运行本脚本
 *   2. 再切到「关注」标签，往下滚几次
 *   3. 再切到「粉丝」标签，往下滚几次
 *   4. 把控制台里所有 [诊断] 开头的输出截图/复制出来
 *
 * 提示：如果自动滚动没生效，手动滚动同样会触发请求，效果一样。
 * ========================================================================== */
(() => {
  'use strict';

  if (window.__dyDiagnose) {
    console.warn('[诊断] 已在运行，直接去切换「关注」/「粉丝」标签并滚动即可。');
    return;
  }

  const hits = { following: 0, fans: 0 };

  function report(url, text) {
    const isFollowing = /\/user\/following\/list\//.test(url);
    const isFans = /\/user\/follower\/list\//.test(url);
    if (!isFollowing && !isFans) return;

    const kind = isFollowing ? 'following' : 'fans';
    hits[kind]++;

    const path = url.split('?')[0];
    const qs = url.includes('?') ? url.slice(url.indexOf('?') + 1) : '';
    const params = {};
    for (const kv of qs.split('&')) {
      if (!kv) continue;
      const i = kv.indexOf('=');
      params[decodeURIComponent(kv.slice(0, i))] = decodeURIComponent(kv.slice(i + 1));
    }

    console.group(`[诊断] #${hits[kind]} ${kind}  ${path}`);

    let json = null;
    try {
      json = JSON.parse(text);
    } catch (e) {
      console.warn('  返回不是 JSON，前 200 字符：', String(text).slice(0, 200));
      console.groupEnd();
      return;
    }

    // 找列表字段：抖音不同版本用过 followings / followers / user_list / data / list
    const CANDIDATES = ['followings', 'followers', 'user_list', 'data', 'list', 'items'];
    const arrKey = CANDIDATES.find((k) => Array.isArray(json[k]));
    const arr = arrKey ? json[arrKey] : [];

    console.log('  顶层字段 :', Object.keys(json).join(', '));
    console.log('  列表字段 :', arrKey || '(没找到数组字段！)');
    console.log('  本页条数 :', arr.length);
    console.log('  总数相关 : total=%s  has_more=%s  max_time=%s  min_time=%s  cursor=%s',
      json.total, json.has_more, json.max_time, json.min_time, json.cursor);
    console.log('  请求参数 :', JSON.stringify(params));

    if (arr.length) {
      const first = arr[0];
      const inner = first.user || first.user_info || first;
      console.log('  单条结构 :', Object.keys(first).join(', '));
      console.log('  内部 user:', Object.keys(inner).join(', '));
      console.log('  条数统计 : uid 有值 %s / sec_uid 有值 %s / nickname 有值 %s',
        arr.filter((x) => (x.user || x.user_info || x).uid).length,
        arr.filter((x) => (x.user || x.user_info || x).sec_uid).length,
        arr.filter((x) => (x.user || x.user_info || x).nickname).length);
      console.log('  样例(截断):', JSON.stringify(first).slice(0, 500));
    }

    console.groupEnd();
  }

  // ---- 拦截 XHR ----
  const oOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (m, u, ...rest) {
    try { this.__dyUrl = String(u || ''); } catch (e) {}
    return oOpen.call(this, m, u, ...rest);
  };
  const oSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function (...args) {
    try {
      const u = this.__dyUrl || '';
      if (/\/(following|follower)\/list\//.test(u)) {
        this.addEventListener('load', () => {
          try {
            const rt = this.responseType;
            if (rt && rt !== 'text' && rt !== 'json') return;
            const t = rt === 'json' ? JSON.stringify(this.response) : this.responseText;
            if (t) report(u, t);
          } catch (e) {}
        });
      }
    } catch (e) {}
    return oSend.apply(this, args);
  };

  // ---- 拦截 fetch ----
  const oFetch = window.fetch;
  window.fetch = function (...args) {
    const p = oFetch.apply(this, args);
    try {
      const u = typeof args[0] === 'string' ? args[0] : args[0] && args[0].url;
      if (u && /\/(following|follower)\/list\//.test(String(u))) {
        p.then((r) => r.clone().text().then((t) => report(String(u), t)).catch(() => {})).catch(() => {});
      }
    } catch (e) {}
    return p;
  };

  window.__dyDiagnose = true;
  window.__dyDiagStatus = () => hits;

  console.log('%c[诊断] 已挂载。现在切到「关注」标签滚动几次，再切到「粉丝」标签滚动几次。',
    'color:#3b82f6;font-weight:bold');
  console.log('  结束后用 __dyDiagStatus() 看一共抓到几次请求。');
  console.log('  如果滚动很久都没有输出 —— 说明接口路径已经不是 /user/following|follower/list/，请到 Network 面板手动找。');
})();
