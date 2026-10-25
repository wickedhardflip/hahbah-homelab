// /ask: streaming chat. POST /api/ask streams newline-delimited JSON ({state}, {token}, {done, chat_id}, {error}).
// Chats live on the server per user (GET /api/ask/chats, /api/ask/chats/{id}). textContent only.
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const log = $("askLog"), form = $("askForm"), input = $("askIn"), go = $("askGo"), pc = $("askPc"), hist = $("askHist"), samples = $("askSamples");
  const WAIT_MS = 3000;   // no token by then = the model is loading (35 to 65 s after idle)
  let chatId = null, busy = false;

  const el = (cls, text) => { const d = document.createElement("div"); d.className = "ask-msg " + cls; d.textContent = text; return d; };
  const add = (cls, text) => { const d = el(cls, text); log.append(d); log.scrollTop = log.scrollHeight; return d; };
  const json = async (url, opt) => { const r = await fetch(url, { credentials: "same-origin", ...opt }); if (!r.ok) throw new Error(r.status); return r.json(); };

  async function status() {
    const labels = { ready: "Home PC: ready", cold: "Home PC: model idle, first answer takes about a minute", nomodel: "Home PC: model missing" };
    try {
      const s = await json("/api/ask/status");
      pc.className = "ask-pc " + (s.state === "ready" || s.state === "cold" ? s.state : "");
      pc.textContent = labels[s.state] || "Home PC: unknown (maybe asleep)";
    } catch (e) { pc.className = "ask-pc"; pc.textContent = "Home PC: unknown"; }
  }

  async function chats() {
    let list = [];
    try { list = (await json("/api/ask/chats")).chats; } catch (e) { /* offline: show an empty list */ }
    hist.replaceChildren(...list.map((c) => {
      const li = document.createElement("li"), b = document.createElement("button"), x = document.createElement("button");
      b.className = "pick"; b.type = "button"; b.textContent = c.title; b.title = c.title;
      if (c.id === chatId) b.setAttribute("aria-current", "true");
      b.addEventListener("click", () => open(c.id));
      x.className = "del"; x.type = "button"; x.textContent = "×"; x.setAttribute("aria-label", "Delete chat");
      x.addEventListener("click", async () => {
        try { await json("/api/ask/chats/" + c.id, { method: "DELETE" }); } catch (e) { return; }
        if (c.id === chatId) fresh();
        chats();
      });
      li.append(b, x);
      return li;
    }));
  }

  function fresh() { chatId = null; log.replaceChildren(); samples.hidden = false; input.focus(); }

  async function open(id) {
    try {
      const d = await json("/api/ask/chats/" + id);
      chatId = id; log.replaceChildren(...d.messages.map((m) => el(m.role, m.content))); samples.hidden = true;
      log.scrollTop = log.scrollHeight; chats();
    } catch (e) { add("bad", "Could not open that chat."); }
  }

  async function ask(q) {
    if (busy || !q.trim()) return;
    busy = true; go.disabled = true; samples.hidden = true;
    add("user", q);
    const reply = add("wait", "Thinking");
    const timer = setTimeout(() => { if (reply.classList.contains("wait")) reply.textContent = "Waking up the home PC, the first answer can take about a minute."; }, WAIT_MS);
    let text = "";
    try {
      const r = await fetch("/api/ask", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: q, chat_id: chatId }) });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const rd = r.body.getReader(), dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await rd.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const lines = buf.split("\n"); buf = lines.pop();
        for (const ln of lines) {
          if (!ln) continue;
          const m = JSON.parse(ln);
          if (m.token) { text += m.token; reply.className = "ask-msg assistant"; reply.textContent = text; log.scrollTop = log.scrollHeight; }
          else if (m.error) { reply.className = "ask-msg bad"; reply.textContent = m.error; }
          else if (m.done) chatId = m.chat_id;
        }
      }
    } catch (e) { if (!text) { reply.className = "ask-msg bad"; reply.textContent = "Could not reach hubbah. Try again."; } }
    clearTimeout(timer); busy = false; go.disabled = false; input.focus();
    status(); chats();
  }

  form.addEventListener("submit", (e) => { e.preventDefault(); const q = input.value; input.value = ""; input.style.height = ""; ask(q); });
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); } });
  input.addEventListener("input", () => { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 120) + "px"; });
  samples.addEventListener("click", (e) => { if (e.target.matches(".chip")) ask(e.target.textContent); });
  $("askNew").addEventListener("click", () => { fresh(); chats(); });
  status(); chats();
})();
