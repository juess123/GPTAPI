/* ==========================================================================
   规格 → 模型   ·   前端交互
   ========================================================================== */

(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const el = {
    drop:       $("drop"),
    picker:     $("picker"),
    filelist:   $("filelist"),
    notice:     $("notice"),
    pasteToast: $("pasteToast"),
    submit:     $("submit"),
    clear:      $("clear"),
    upload:     $("uploadCard"),
    prompt:     $("userPrompt"),
    promptCount: $("promptCount"),

    progCard:   $("progressCard"),
    stageLabel: $("stageLabel"),
    stageSub:   $("stageSub"),
    pct:        $("pctLabel"),
    bar:        $("bar"),
    barFill:    $("barFill"),
    progNotice: $("progNotice"),
    elapsed:    $("elapsed"),
    toggleLog:  $("toggleLog"),
    console:    $("console"),
    logBody:    $("logBody"),

    resCard:    $("resultCard"),
    resSub:     $("resultSub"),
    dlGrid:     $("dlGrid"),
    extras:     $("extras"),

    failCard:   $("failCard"),
    failMsg:    $("failMsg"),
    failLog:    $("failLog"),

    hist:       $("histList"),
  };

  let LIMITS = { imageLimitMB: 4, accept: [] };
  let rejectedByClient = [];
  let picked = [];             // { file, name, size, kind }
  let currentJob = null;
  let es = null;
  let timer = null;
  let t0 = 0;

  // ── 阶段文案 ──────────────────────────────────────────────
  const STAGE_TEXT = {
    "排队中":         ["已排队",     "前面还有任务在跑，马上开始"],
    "解析上传材料":   ["正在解析材料", "读取图片、规格文档与价目表"],
    "模型生成脚本中": ["模型生成脚本中", "正在通读资料并编写生成脚本，通常几分钟"],
    "模型脚本已就绪": ["脚本已生成",  "即将在本机执行"],
    "生成 Excel 报价表": ["生成 Excel 报价表", "本机执行脚本中"],
    "校验成品":       ["校验成品",   "确认文件真实可读"],
    "已完成":         ["已完成",     "两个成品已就绪"],
    "已中断":         ["已中断",     "服务重启导致任务中断"],
  };

  // ── 工具 ─────────────────────────────────────────────────

  const fmtSize = (n) => {
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
    if (n < 1073741824) return (n / 1048576).toFixed(1) + " MB";
    return (n / 1073741824).toFixed(2) + " GB";
  };

  const fmtClock = (s) => {
    s = Math.max(0, Math.floor(s));
    const m = Math.floor(s / 60);
    return m ? `${m}:${String(s % 60).padStart(2, "0")}` : `${s}s`;
  };

  // 任务「几点开始、几点结束」的墙上时间。
  // 新版服务端会在快照里带 started / finished；老服务端只有 created + elapsed，
  // 那就退化成「提交时刻 + 耗时」——排队久的任务会差几分钟，重启服务端后就准了。
  const pad2 = (n) => String(n).padStart(2, "0");
  const clockOf = (d) => `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
  const dayOf = (d) => `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;

  function jobSpan(job, secs) {
    const start = job.started ?? job.created ?? Date.now() / 1000 - secs;
    const end = job.finished ?? start + secs;
    const d1 = new Date(start * 1000);
    const d2 = new Date(end * 1000);
    // 跨天（夜里跑的任务）才把结束那边的日期也写出来，平时省两个字符
    const cross = d1.toDateString() !== d2.toDateString();
    return `开始 ${dayOf(d1)} ${clockOf(d1)} · 结束 ${cross ? dayOf(d2) + " " : ""}${clockOf(d2)}`;
  }

  const extOf = (name) => {
    const i = name.lastIndexOf(".");
    return i < 0 ? "" : name.slice(i).toLowerCase();
  };

  const kindOf = (name) => {
    const e = extOf(name);
    if ([".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"].includes(e)) return "image";
    if ([".xlsx", ".xlsm"].includes(e)) return "xlsx";
    return "text";
  };

  const BADGE = { image: "IMG", xlsx: "XLS", text: "MD" };
  const KIND_CN = { image: "图片", xlsx: "价格表", text: "规格文本" };

  // ── 文件列表渲染 ──────────────────────────────────────────

  function renderFiles() {
    el.filelist.innerHTML = "";
    el.filelist.hidden = picked.length === 0;

    picked.forEach((p, i) => {
      const li = document.createElement("li");
      const kb = kindOf(p.name);

      const badge = document.createElement("span");
      badge.className = "badge " + kb;
      badge.textContent = BADGE[kb];

      const name = document.createElement("span");
      name.className = "f-name";
      name.textContent = p.name;

      const meta = document.createElement("span");
      meta.className = "f-meta";
      meta.textContent = `${KIND_CN[kb]} · ${fmtSize(p.size)}`;

      const del = document.createElement("button");
      del.className = "f-del";
      del.type = "button";
      del.textContent = "×";
      del.title = "移除";
      del.onclick = () => { picked.splice(i, 1); renderFiles(); };

      li.append(badge, name, meta, del);
      el.filelist.append(li);
    });

    const promptText = el.prompt.value.trim();
    const hasInput = picked.length > 0 || promptText.length > 0;
    el.submit.disabled = !hasInput;
    el.clear.hidden = !hasInput;
    el.promptCount.textContent = `${el.prompt.value.length} / 20000`;

    const total = picked.reduce((s, p) => s + p.size, 0);
    el.submit.querySelector("span").textContent = picked.length
      ? `开始生成　·　${picked.length} 个文件 · ${fmtSize(total)}`
      : (promptText ? "开始生成　·　仅文字" : "开始生成");

    // 上传大小不限；这里只提示模型网关不会直接接收过大的图片。
    const warn = [];
    if (rejectedByClient.length) {
      warn.push("已忽略：" + rejectedByClient.join("、"));
    }
    const bigImg = picked.filter((p) => kindOf(p.name) === "image"
      && p.size > LIMITS.imageLimitMB * 1048576);
    if (bigImg.length) {
      warn.push(`${bigImg.map((p) => p.name).join("、")} 超过 ${LIMITS.imageLimitMB}MB，将自动优化副本后发送，原图保持不变。`);
    }
    el.notice.hidden = warn.length === 0;
    el.notice.textContent = warn.join(" ");
  }

  function addFiles(list) {
    const seen = new Set(picked.map((p) => p.name));
    rejectedByClient = [];
    for (const f of list) {
      if (!f || !f.name) continue;

      // 按扩展名先筛一道，免得白传上来再被服务端拒掉
      if (LIMITS.accept.length && !LIMITS.accept.includes(extOf(f.name))) {
        rejectedByClient.push(`${f.name}（不支持的格式）`);
        continue;
      }
      let name = f.name;
      if (seen.has(name)) {
        const e = extOf(name);
        const stem = e ? name.slice(0, -e.length) : name;
        let n = 2;
        while (seen.has(`${stem}(${n})${e}`)) n++;
        name = `${stem}(${n})${e}`;
      }
      seen.add(name);
      picked.push({ file: f, name, size: f.size });
    }
    renderFiles();
  }

  // ── 拖放 ──────────────────────────────────────────────────

  let depth = 0;
  ["dragenter", "dragover"].forEach((ev) =>
    el.drop.addEventListener(ev, (e) => {
      e.preventDefault();
      if (ev === "dragenter") depth++;
      el.drop.classList.add("over");
    }));
  ["dragleave", "drop"].forEach((ev) =>
    el.drop.addEventListener(ev, (e) => {
      e.preventDefault();
      if (ev === "dragleave") { depth--; if (depth > 0) return; }
      depth = 0;
      el.drop.classList.remove("over");
    }));
  el.drop.addEventListener("drop", (e) => {
    const files = [...(e.dataTransfer?.files || [])];
    if (files.length) addFiles(files);
  });

  el.drop.addEventListener("click", () => el.picker.click());
  el.drop.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); el.picker.click(); }
  });
  el.picker.addEventListener("change", () => {
    addFiles([...el.picker.files]);
    el.picker.value = "";
  });

  el.prompt.addEventListener("input", renderFiles);
  el.clear.onclick = () => { picked = []; el.prompt.value = ""; renderFiles(); };

  // 整页拖放也要接住，避免浏览器直接打开文件
  ["dragover", "drop"].forEach((ev) =>
    window.addEventListener(ev, (e) => {
      if (!e.target.closest?.("#drop")) e.preventDefault();
    }));

  // ── 粘贴 ──────────────────────────────────────────────────
  // 截图（Win+Shift+S）、网页上复制的图、画图工具里的图，都能直接 Ctrl+V 进来。
  // 剪贴板里只有文字时（比如从 Excel 里选一片单元格复制的），存成一个 .md 当规格文本。

  const MIME_EXT = {
    "image/png":     ".png",
    "image/jpeg":    ".jpg",
    "image/webp":    ".webp",
    "image/gif":     ".gif",
    "image/bmp":     ".bmp",
    "image/x-ms-bmp": ".bmp",
  };
  // 浏览器给粘贴图片的默认名，没有信息量，换成我们自己编号的名字
  const GENERIC = new Set(["", "image", "image.png", "image.jpg", "image.jpeg",
                           "blob", "untitled", "clipboard"]);
  let pasteSeq = 0;
  let toastTimer = null;

  const allowed = (x) => !LIMITS.accept.length || LIMITS.accept.includes(x);

  function pasteNote(msg, bad) {
    el.pasteToast.textContent = msg;
    el.pasteToast.classList.toggle("bad", !!bad);
    el.pasteToast.hidden = false;
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.pasteToast.hidden = true; }, 3600);
  }

  function flashDrop() {
    el.drop.classList.remove("flash");
    void el.drop.offsetWidth;          // 重排一次，连着粘两次也能各闪一下
    el.drop.classList.add("flash");
    setTimeout(() => el.drop.classList.remove("flash"), 900);
  }

  document.addEventListener("paste", (e) => {
    if (el.upload.hidden) return;      // 任务在跑 / 正在看结果时不抢粘贴
    if (e.target === el.prompt || e.target?.isContentEditable) return;
    const cd = e.clipboardData;
    if (!cd) return;

    const raw = [];
    for (const it of cd.items || []) {
      if (it.kind !== "file") continue;
      const f = it.getAsFile();
      if (f) raw.push(f);
    }
    if (!raw.length) raw.push(...(cd.files || []));   // Firefox 这里更靠谱

    const batch = [];
    const bad = [];

    if (raw.length) {
      for (const f of raw) {
        const ext = MIME_EXT[f.type] || extOf(f.name || "");
        if (!ext || !allowed(ext)) { bad.push(f.type || f.name || "未知类型"); continue; }
        const given = (f.name || "").trim();
        const keep = given && !GENERIC.has(given.toLowerCase()) && allowed(extOf(given));
        const name = keep ? given : `粘贴图片-${++pasteSeq}${ext}`;
        batch.push(new File([f], name, { type: f.type, lastModified: f.lastModified }));
      }
      if (!batch.length) {
        pasteNote(`剪贴板里的内容（${bad.join("、")}）不是支持的图片格式，没加进来`, true);
        return;
      }
    } else {
      const text = (cd.getData("text/plain") || "").trim();
      if (text.length < 20) return;    // 太短，多半是误触，不拦这一下粘贴
      batch.push(new File([text], `粘贴文本-${++pasteSeq}.md`, { type: "text/markdown" }));
    }

    e.preventDefault();
    addFiles(batch);
    flashDrop();
    pasteNote("已从剪贴板添加：" + batch.map((f) => f.name).join("、")
              + (bad.length ? `；跳过 ${bad.join("、")}` : ""));
  });

  // ── 进度面板 ──────────────────────────────────────────────

  function setProgress(p) {
    const v = Math.max(0, Math.min(100, p));
    el.barFill.style.width = v + "%";
    el.pct.innerHTML = `${Math.round(v)}<i>%</i>`;
  }

  function setStage(stage) {
    const [title, sub] = STAGE_TEXT[stage] || [stage || "处理中", ""];
    el.stageLabel.textContent = title;
    el.stageSub.textContent = sub;
    const running = !["已完成", "已中断"].includes(stage);
    el.bar.classList.toggle("active", running);
  }

  function appendLog(line) {
    const pre = el.logBody;
    const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
    pre.textContent += (pre.textContent ? "\n" : "") + line;
    if (pre.textContent.length > 60000) pre.textContent = pre.textContent.slice(-50000);
    if (atBottom) pre.scrollTop = pre.scrollHeight;
  }

  function startClock() {
    t0 = Date.now();
    stopClock();
    timer = setInterval(() => {
      el.elapsed.textContent = fmtClock((Date.now() - t0) / 1000);
    }, 1000);
    el.elapsed.textContent = "0s";
  }
  function stopClock() { if (timer) { clearInterval(timer); timer = null; } }

  el.toggleLog.onclick = () => {
    el.console.hidden = !el.console.hidden;
    el.toggleLog.textContent = el.console.hidden ? "显示日志" : "隐藏日志";
    if (!el.console.hidden) el.logBody.scrollTop = el.logBody.scrollHeight;
  };

  // ── 结果渲染 ──────────────────────────────────────────────

  function renderResult(job) {
    stopClock();
    el.progCard.hidden = true;
    el.failCard.hidden = true;

    const secs = job.elapsed ?? (Date.now() - t0) / 1000;
    el.resSub.textContent = jobSpan(job, secs)
      + `　·　耗时 ${fmtClock(secs)}　·　共 ${job.files.length} 个文件`;

    const primary = job.files.filter((f) => f.primary);
    const extras = job.files.filter((f) => !f.primary);

    el.dlGrid.innerHTML = "";
    for (const f of primary) {
      const isBlend = f.name.endsWith(".blend");
      const a = document.createElement("a");
      a.className = "dl";
      a.href = `/api/jobs/${job.id}/download/${encodeURIComponent(f.name)}`;
      a.setAttribute("download", f.name);

      const ico = document.createElement("span");
      ico.className = "dl-ico " + (isBlend ? "blend" : "xlsx");
      ico.textContent = isBlend ? "BLD" : "XLS";

      const txt = document.createElement("div");
      txt.className = "dl-txt";
      const nm = document.createElement("div");
      nm.className = "dl-name";
      nm.textContent = f.name;
      const sz = document.createElement("div");
      sz.className = "dl-size";
      // 文案要短：右边还有一颗下载按钮，太长会被截断成省略号
      sz.textContent = (isBlend ? "Blender 文件" : "Excel 工作表")
        + " · " + fmtSize(f.size);
      txt.append(nm, sz);

      // 显式的下载按钮：整块磁贴本来就能点，但按钮一眼就看得出该干嘛
      const btn = document.createElement("span");
      btn.className = "dl-btn";
      btn.textContent = "下载";

      a.append(ico, txt, btn);
      el.dlGrid.append(a);
    }

    if (extras.length) {
      el.extras.hidden = false;
      el.extras.innerHTML = "<span>另有附属文件：</span>";
      for (const f of extras) {
        const a = document.createElement("a");
        a.href = `/api/jobs/${job.id}/download/${encodeURIComponent(f.name)}`;
        a.textContent = `${f.name}（${fmtSize(f.size)}）`;
        el.extras.append(a);
      }
    } else {
      el.extras.hidden = true;
    }

    // 底部「再次上传」
    let again = el.resCard.querySelector(".again-row");
    if (!again) {
      again = document.createElement("div");
      again.className = "actions again-row";
      const b = document.createElement("button");
      b.className = "btn btn-rect";
      b.textContent = "再次上传";
      b.onclick = resetUI;
      again.append(b);
      el.resCard.append(again);
    }

    el.resCard.hidden = false;
    el.resCard.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  function renderFail(job) {
    stopClock();
    el.progCard.hidden = true;
    el.resCard.hidden = true;
    el.failMsg.textContent = job.error || "任务未完成";
    el.failLog.textContent = (job.logs || []).join("\n");

    let again = el.failCard.querySelector(".again-row");
    if (!again) {
      again = document.createElement("div");
      again.className = "actions again-row";
      const b = document.createElement("button");
      b.className = "btn btn-rect";
      b.textContent = "重新开始";
      b.onclick = resetUI;
      again.append(b);
      el.failCard.append(again);
    }

    el.failCard.hidden = false;
    el.failCard.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  function resetUI() {
    if (es) { es.close(); es = null; }
    stopClock();
    currentJob = null;
    el.progCard.hidden = true;
    el.resCard.hidden = true;
    el.failCard.hidden = true;
    el.console.hidden = true;
    el.progNotice.hidden = true;
    el.toggleLog.textContent = "显示日志";
    el.logBody.textContent = "";
    el.upload.hidden = false;
    el.pasteToast.hidden = true;
    picked = [];
    el.prompt.value = "";
    renderFiles();
    el.upload.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  // ── 提交 ─────────────────────────────────────────────────

  el.submit.onclick = () => {
    const promptText = el.prompt.value.trim();
    if (!picked.length && !promptText) return;

    const fd = new FormData();
    for (const p of picked) fd.append("files", p.file, p.name);
    fd.append("prompt", promptText);

    el.submit.disabled = true;
    el.submit.querySelector("span").textContent = "正在上传…";

    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/jobs");

    xhr.upload.onprogress = (e) => {
      if (!e.lengthComputable) return;
      const p = Math.round((e.loaded / e.total) * 100);
      el.submit.querySelector("span").textContent = `正在上传… ${p}%`;
    };

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        let data = {};
        try { data = JSON.parse(xhr.responseText); } catch (_) {}
        beginJob(data);
      } else {
        let msg = `上传失败（HTTP ${xhr.status}）`;
        try {
          const d = JSON.parse(xhr.responseText);
          if (d.detail) msg = typeof d.detail === "string" ? d.detail : JSON.stringify(d.detail);
        } catch (_) {}
        showUploadError(msg);
      }
    };
    xhr.onerror = () => showUploadError("网络错误，无法连接后端");
    xhr.send(fd);
  };

  function showUploadError(msg) {
    el.submit.disabled = false;
    renderFiles();                      // 先让它算出本地预警，再覆盖成服务端错误
    el.notice.hidden = false;
    el.notice.textContent = msg;
  }

  function beginJob(data) {
    // 不把上传卡片藏起来：并发上限是 2，同一个页面可以连着排几个任务，
    // 多出来的在服务端排队。所以提交完只是清空选择区，让它立刻能接下一批。
    picked = [];
    rejectedByClient = [];
    el.prompt.value = "";
    renderFiles();

    focusJob({ id: data.id, stage: "排队中", progress: 4 });

    const skipped = data.warnings || [];
    el.progNotice.hidden = skipped.length === 0;
    el.progNotice.textContent = skipped.length
      ? "以下文件被跳过：" + skipped.join("；")
      : "";

    refreshHistory();
  }

  // 把进度面板切到某个任务上：新提交的，或从历史里点开的排队中 / 运行中的任务
  function focusJob(job) {
    currentJob = job.id;
    el.resCard.hidden = true;
    el.failCard.hidden = true;
    el.progCard.hidden = false;
    el.logBody.textContent = "";
    setStage(job.stage || "排队中");
    setProgress(typeof job.progress === "number" ? job.progress : 0);
    startClock();
    el.progCard.scrollIntoView({ behavior: "smooth", block: "center" });
    watch(job.id);
  }

  // ── SSE ──────────────────────────────────────────────────

  function watch(jobId) {
    if (es) es.close();
    es = new EventSource(`/api/jobs/${jobId}/events`);

    es.onmessage = (ev) => {
      let d;
      try { d = JSON.parse(ev.data); } catch (_) { return; }

      if (d.type === "snapshot") {
        setStage(d.stage);
        setProgress(d.progress);
        if (Array.isArray(d.logs) && d.logs.length) {
          el.logBody.textContent = d.logs.join("\n");
          el.logBody.scrollTop = el.logBody.scrollHeight;
        }
        if (d.status === "done") { es.close(); renderResult(d); }
        if (d.status === "failed") { es.close(); renderFail(d); }
        return;
      }

      if (d.type === "stage") {
        setStage(d.stage);
        if (typeof d.progress === "number") setProgress(d.progress);
        return;
      }

      if (d.type === "log") { appendLog(d.line); return; }

      if (d.type === "end") {
        es.close(); es = null;
        refreshHistory();
        const final = { ...d, id: d.id || jobId };
        if (d.status === "done") renderResult(final); else renderFail(final);
      }
    };

    es.onerror = () => {
      // 任务真的结束时会主动关闭；这里只做兜底重连
      if (es && es.readyState === EventSource.CLOSED) {
        fetch(`/api/jobs/${jobId}`).then((r) => r.json()).then((j) => {
          if (j.status === "done") renderResult(j);
          else if (j.status === "failed") renderFail(j);
        }).catch(() => {});
      }
    };
  }

  // ── 历史 ─────────────────────────────────────────────────

  async function refreshHistory() {
    let data;
    try {
      data = await (await fetch("/api/jobs")).json();
    } catch (_) { return; }

    const jobs = (data.jobs || []).slice(0, 12);

    el.hist.innerHTML = "";
    if (!jobs.length) {
      el.hist.innerHTML = '<li class="empty">还没有记录</li>';
      return;
    }

    for (const j of jobs) {
      const li = document.createElement("li");

      const dot = document.createElement("span");
      dot.className = "dot " + j.status;

      // 右栏窄，分两行放：上行任务号，下行状态与输入数量
      const body = document.createElement("div");
      body.className = "hist-body";

      const id = document.createElement("span");
      id.className = "hist-id";
      id.textContent = j.id;

      const meta = document.createElement("span");
      meta.className = "hist-meta";

      const st = document.createElement("span");
      st.className = "hist-stage";
      st.textContent = j.status === "done"
        ? `完成 · ${fmtClock(j.elapsed)}`
        : (j.status === "failed"
            ? "失败"
            : (j.stage || j.status) + (j.status === "running" ? "　运行中" : ""));

      const inp = document.createElement("span");
      inp.className = "hist-stage";
      inp.textContent = `${(j.inputs || []).length} 个输入`;

      meta.append(st, inp);
      body.append(id, meta);
      li.append(dot, body);
      li.style.cursor = "pointer";
      li.title = j.id;
      li.onclick = () => {
        if (j.status === "done") {
          if (es) { es.close(); es = null; }
          currentJob = j.id;
          renderResult(j);
        } else if (j.status === "failed") {
          if (es) { es.close(); es = null; }
          currentJob = j.id;
          renderFail(j);
        } else {
          // 排队中 / 运行中：切过去看实时进度
          focusJob(j);
        }
      };
      el.hist.append(li);
    }
  }

  // ── 启动 ─────────────────────────────────────────────────

  (async function init() {
    try {
      const c = await (await fetch("/api/config")).json();
      LIMITS = { ...LIMITS, ...c };
      el.picker.setAttribute("accept", (c.accept || []).join(","));
    } catch (_) {}
    renderFiles();
    refreshHistory();
    setInterval(refreshHistory, 15000);
    window.__resetUI = resetUI;
  })();

})();
