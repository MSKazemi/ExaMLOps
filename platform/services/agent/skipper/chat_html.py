"""Self-contained HTML/CSS/JS for the Skipper (ExaMLOps agent) chat interface."""

CHAT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Skipper · ExaMLOps</title>
<script src="https://cdn.jsdelivr.net/npm/marked@13/marked.min.js"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.10.0/styles/github-dark.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.10.0/highlight.min.js"></script>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --bg:        #0d1117;
  --bg2:       #161b22;
  --bg3:       #1c2128;
  --border:    #30363d;
  --text:      #e6edf3;
  --muted:     #8b949e;
  --accent:    #58a6ff;
  --accent-dim:#1f3a5f;
  --human-bg:  #0c3d60;
  --human-bd:  #1b5e8a;
  --ai-bg:     #1c2128;
  --ai-bd:     #30363d;
  --tool-bg:   #0d2137;
  --tool-text: #58a6ff;
  --success:   #3fb950;
  --warn:      #d29922;
  --error:     #f85149;
  --radius:    12px;
  --sidebar-w: 260px;
  font-size: 14px;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
  color-scheme: dark;
}

body { background: var(--bg); color: var(--text); height: 100vh; overflow: hidden; }

#app {
  display: grid;
  grid-template-columns: var(--sidebar-w) 1fr;
  height: 100vh;
}

/* ── Sidebar ── */
#sidebar {
  background: var(--bg2);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

#sidebar-header {
  padding: 16px;
  border-bottom: 1px solid var(--border);
}

#sidebar-header h2 {
  font-size: 13px;
  font-weight: 600;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: var(--muted);
  margin-bottom: 8px;
}

#model-badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  background: var(--accent-dim);
  color: var(--accent);
  border: 1px solid #1f6feb;
  border-radius: 20px;
  padding: 3px 10px;
  font-size: 11px;
  font-weight: 500;
}

#model-badge::before {
  content: '';
  width: 6px; height: 6px;
  border-radius: 50%;
  background: var(--success);
  animation: pulse 2s infinite;
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}

#new-thread-btn {
  margin: 12px;
  padding: 8px 14px;
  background: var(--accent);
  color: #000;
  border: none;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  width: calc(100% - 24px);
  transition: background 0.15s, transform 0.1s;
  text-align: center;
}
#new-thread-btn:hover { background: #79c0ff; }
#new-thread-btn:active { transform: scale(0.97); }

#thread-list {
  flex: 1;
  overflow-y: auto;
  padding: 4px 8px;
  list-style: none;
}

#thread-list li {
  padding: 8px 10px;
  border-radius: 8px;
  cursor: pointer;
  font-size: 12px;
  color: var(--muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  transition: background 0.1s, color 0.1s;
  user-select: none;
}
#thread-list li:hover { background: var(--bg3); color: var(--text); }
#thread-list li.active {
  background: var(--accent-dim);
  color: var(--accent);
  font-weight: 500;
}
#thread-list li .thread-id { font-family: monospace; }
#thread-list li .thread-preview {
  color: var(--muted);
  font-size: 11px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* ── Main chat ── */
#chat-main {
  display: flex;
  flex-direction: column;
  height: 100vh;
  overflow: hidden;
}

#chat-header {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 20px;
  border-bottom: 1px solid var(--border);
  background: var(--bg2);
  flex-shrink: 0;
}

#chat-thread-label {
  font-size: 12px;
  color: var(--muted);
  font-family: monospace;
  flex: 1;
}

#connection-dot {
  width: 8px; height: 8px;
  border-radius: 50%;
  background: var(--error);
  transition: background 0.3s;
}
#connection-dot.connected { background: var(--success); }
#connection-dot.connecting { background: var(--warn); animation: pulse 1s infinite; }

#messages {
  flex: 1;
  overflow-y: auto;
  padding: 20px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  scroll-behavior: smooth;
}

/* ── Message bubbles ── */
.msg-row {
  display: flex;
  gap: 10px;
  max-width: 85%;
  animation: fadeSlideUp 0.2s ease-out;
}

@keyframes fadeSlideUp {
  from { opacity: 0; transform: translateY(8px); }
  to   { opacity: 1; transform: translateY(0); }
}

.msg-row.human { align-self: flex-end; flex-direction: row-reverse; }
.msg-row.ai    { align-self: flex-start; }

.avatar {
  width: 28px; height: 28px;
  border-radius: 50%;
  flex-shrink: 0;
  display: flex; align-items: center; justify-content: center;
  font-size: 13px;
  font-weight: 700;
  margin-top: 2px;
}
.avatar.ai    { background: linear-gradient(135deg, #1f6feb, #58a6ff); color: #fff; }
.avatar.human { background: linear-gradient(135deg, #0c3d60, #1b5e8a); color: #7cc6ff; }

.bubble {
  border-radius: var(--radius);
  padding: 12px 16px;
  line-height: 1.6;
  word-break: break-word;
  position: relative;
}

.bubble.ai {
  background: var(--ai-bg);
  border: 1px solid var(--ai-bd);
  border-top-left-radius: 4px;
}

.bubble.human {
  background: var(--human-bg);
  border: 1px solid var(--human-bd);
  border-top-right-radius: 4px;
  color: #c9e3f8;
}

.bubble.streaming::after {
  content: '▍';
  color: var(--accent);
  animation: blink 0.8s step-end infinite;
  margin-left: 2px;
}

@keyframes blink {
  0%, 100% { opacity: 1; }
  50% { opacity: 0; }
}

/* Markdown in bubbles */
.bubble p { margin-bottom: 10px; }
.bubble p:last-child { margin-bottom: 0; }
.bubble ul, .bubble ol { padding-left: 20px; margin-bottom: 10px; }
.bubble li { margin-bottom: 4px; }
.bubble h1,.bubble h2,.bubble h3 {
  font-size: 1em; font-weight: 600; margin: 14px 0 6px;
  color: var(--accent);
}
.bubble h1 { font-size: 1.15em; border-bottom: 1px solid var(--border); padding-bottom: 4px; }
.bubble code {
  background: rgba(110,118,129,0.15);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 2px 5px;
  font-family: 'SFMono-Regular', Consolas, monospace;
  font-size: 0.88em;
}
.bubble pre {
  background: #010409 !important;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px;
  overflow-x: auto;
  margin: 10px 0;
}
.bubble pre code {
  background: none !important;
  border: none !important;
  padding: 0;
  font-size: 0.85em;
}
.bubble table {
  border-collapse: collapse;
  width: 100%;
  margin: 10px 0;
  font-size: 0.9em;
}
.bubble th, .bubble td {
  border: 1px solid var(--border);
  padding: 6px 12px;
  text-align: left;
}
.bubble th { background: var(--bg3); color: var(--accent); font-weight: 600; }
.bubble tr:nth-child(even) { background: rgba(255,255,255,0.02); }
.bubble blockquote {
  border-left: 3px solid var(--accent);
  padding-left: 12px;
  color: var(--muted);
  margin: 8px 0;
}
.bubble a { color: var(--accent); }

/* Bubble meta (copy btn + usage) */
.bubble-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px solid rgba(255,255,255,0.05);
  font-size: 11px;
  color: var(--muted);
}
.copy-btn {
  background: none;
  border: 1px solid var(--border);
  border-radius: 4px;
  color: var(--muted);
  padding: 2px 7px;
  font-size: 10px;
  cursor: pointer;
  transition: all 0.15s;
}
.copy-btn:hover { border-color: var(--accent); color: var(--accent); }
.copy-btn.copied { border-color: var(--success); color: var(--success); }

/* ── Tool call chips ── */
.tool-group {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 4px 38px;
  align-self: flex-start;
}

.tool-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  background: var(--tool-bg);
  border: 1px solid #1b3a5c;
  border-radius: 20px;
  padding: 3px 10px;
  font-size: 11px;
  color: var(--tool-text);
  font-family: monospace;
  animation: fadeSlideUp 0.15s ease-out;
}
.tool-chip::before {
  content: '⚙';
  font-size: 10px;
}
.tool-chip.running::before {
  content: '';
  width: 8px; height: 8px;
  border-radius: 50%;
  border: 1.5px solid var(--tool-text);
  border-top-color: transparent;
  animation: spin 0.7s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* ── Typing indicator ── */
.typing-indicator {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 12px 16px;
  background: var(--ai-bg);
  border: 1px solid var(--ai-bd);
  border-radius: var(--radius);
  border-top-left-radius: 4px;
  width: 60px;
}
.typing-indicator span {
  width: 6px; height: 6px;
  border-radius: 50%;
  background: var(--muted);
  animation: bounce 1.2s ease-in-out infinite;
}
.typing-indicator span:nth-child(2) { animation-delay: 0.2s; }
.typing-indicator span:nth-child(3) { animation-delay: 0.4s; }
@keyframes bounce {
  0%, 80%, 100% { transform: translateY(0); }
  40% { transform: translateY(-6px); }
}

/* ── Input area ── */
#input-area {
  flex-shrink: 0;
  padding: 16px 20px;
  background: var(--bg2);
  border-top: 1px solid var(--border);
  display: flex;
  gap: 10px;
  align-items: flex-end;
}

#input {
  flex: 1;
  min-height: 44px;
  max-height: 160px;
  background: var(--bg3);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 11px 14px;
  color: var(--text);
  font-size: 14px;
  font-family: inherit;
  resize: none;
  outline: none;
  line-height: 1.5;
  transition: border-color 0.15s;
  overflow-y: auto;
}
#input:focus { border-color: var(--accent); }
#input::placeholder { color: var(--muted); }

#send-btn {
  flex-shrink: 0;
  width: 44px; height: 44px;
  background: var(--accent);
  border: none;
  border-radius: 10px;
  cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  transition: background 0.15s, transform 0.1s, opacity 0.15s;
  color: #000;
}
#send-btn:hover:not(:disabled) { background: #79c0ff; }
#send-btn:active:not(:disabled) { transform: scale(0.94); }
#send-btn:disabled { opacity: 0.4; cursor: not-allowed; }
#send-btn svg { width: 18px; height: 18px; fill: currentColor; }

/* ── Interrupt modal ── */
#interrupt-overlay {
  display: none;
  position: fixed; inset: 0;
  background: rgba(0,0,0,0.7);
  z-index: 100;
  align-items: center;
  justify-content: center;
  backdrop-filter: blur(4px);
}
#interrupt-overlay.visible { display: flex; }

#interrupt-modal {
  background: var(--bg2);
  border: 1px solid var(--warn);
  border-radius: 14px;
  padding: 28px 32px;
  max-width: 500px;
  width: 90%;
  animation: fadeSlideUp 0.2s ease-out;
}

#interrupt-modal h3 {
  color: var(--warn);
  font-size: 14px;
  font-weight: 600;
  margin-bottom: 12px;
  display: flex; align-items: center; gap: 8px;
}
#interrupt-modal h3::before { content: '⚠'; font-size: 16px; }

#interrupt-action {
  font-family: monospace;
  background: var(--bg3);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px 12px;
  font-size: 12px;
  color: var(--muted);
  margin-bottom: 6px;
  white-space: pre-wrap;
  word-break: break-all;
}
#interrupt-summary {
  font-size: 13px;
  color: var(--text);
  margin-bottom: 20px;
  line-height: 1.5;
}

.modal-btns { display: flex; gap: 10px; justify-content: flex-end; }

.modal-btn {
  padding: 8px 20px;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  border: 1px solid transparent;
  transition: all 0.15s;
}
#modal-confirm {
  background: var(--error);
  color: #fff;
  border-color: var(--error);
}
#modal-confirm:hover { background: #ff6b6b; }
#modal-cancel {
  background: var(--bg3);
  color: var(--text);
  border-color: var(--border);
}
#modal-cancel:hover { border-color: var(--text); }

/* ── Empty state ── */
#empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  flex: 1;
  color: var(--muted);
  text-align: center;
  padding: 40px;
  gap: 12px;
}
#empty-state .logo {
  font-size: 40px;
  margin-bottom: 8px;
}
#empty-state h3 { font-size: 18px; font-weight: 600; color: var(--text); }
#empty-state p { font-size: 13px; max-width: 360px; line-height: 1.6; }

.suggestion-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: center;
  margin-top: 8px;
}
.suggestion-chip {
  background: var(--bg3);
  border: 1px solid var(--border);
  border-radius: 20px;
  padding: 6px 14px;
  font-size: 12px;
  cursor: pointer;
  color: var(--text);
  transition: all 0.15s;
}
.suggestion-chip:hover {
  background: var(--accent-dim);
  border-color: var(--accent);
  color: var(--accent);
}

/* ── Scrollbar ── */
::-webkit-scrollbar { width: 5px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: var(--muted); }
</style>
</head>
<body>
<div id="app">

  <!-- Sidebar -->
  <aside id="sidebar">
    <div id="sidebar-header">
      <h2>Skipper</h2>
      <div id="model-badge">Loading...</div>
    </div>
    <button id="new-thread-btn" onclick="newThread()">+ New Chat</button>
    <ul id="thread-list"></ul>
  </aside>

  <!-- Main chat panel -->
  <main id="chat-main">
    <div id="chat-header">
      <span id="chat-thread-label">Select a thread or start a new chat</span>
      <div id="connection-dot" title="Disconnected"></div>
    </div>

    <div id="messages">
      <div id="empty-state">
        <div class="logo">🤖</div>
        <h3>ExaMLOps Platform Agent</h3>
        <p>Ask about model health, drift, approvals, training runs, or anything about your MLOps platform.</p>
        <div class="suggestion-chips">
          <div class="suggestion-chip" onclick="quickSend(this)">Platform health status?</div>
          <div class="suggestion-chip" onclick="quickSend(this)">Any drift alerts?</div>
          <div class="suggestion-chip" onclick="quickSend(this)">List production models</div>
          <div class="suggestion-chip" onclick="quickSend(this)">Pending approvals?</div>
          <div class="suggestion-chip" onclick="quickSend(this)">Run platform diagnostic</div>
          <div class="suggestion-chip" onclick="quickSend(this)">Generate status report</div>
        </div>
      </div>
    </div>

    <div id="input-area">
      <textarea id="input"
        placeholder="Ask about platform health, models, drift, approvals..."
        rows="1"></textarea>
      <button id="send-btn" onclick="sendMessage()" disabled title="Send (Enter)">
        <svg viewBox="0 0 24 24"><path d="M2 21l21-9L2 3v7l15 2-15 2z"/></svg>
      </button>
    </div>
  </main>
</div>

<!-- Interrupt confirmation modal -->
<div id="interrupt-overlay">
  <div id="interrupt-modal">
    <h3>Confirm Action</h3>
    <div id="interrupt-action"></div>
    <div id="interrupt-summary"></div>
    <div class="modal-btns">
      <button class="modal-btn" id="modal-cancel" onclick="replyInterrupt(false)">Cancel</button>
      <button class="modal-btn" id="modal-confirm" onclick="replyInterrupt(true)">Confirm</button>
    </div>
  </div>
</div>

<script>
'use strict';

// ── State ─────────────────────────────────────────────────────────────────────
let ws = null;
let currentThread = null;
let isStreaming = false;
let currentBubble = null;       // the .bubble element being streamed into
let currentToolGroup = null;    // the .tool-group shown during streaming
let currentRaw = '';            // raw markdown accumulator for current message
let typingIndicatorRow = null;  // the typing dots row

// ── Init ──────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  await loadInfo();
  await loadThreads();
  setupInput();
});

async function loadInfo() {
  try {
    const r = await fetch('/api/info');
    const d = await r.json();
    const badge = document.getElementById('model-badge');
    badge.textContent = d.model || 'unknown';
    if (!d.ok) badge.style.background = 'var(--error)';
  } catch(e) {
    document.getElementById('model-badge').textContent = 'offline';
  }
}

async function loadThreads() {
  try {
    const r = await fetch('/api/threads');
    const d = await r.json();
    renderThreadList(d.threads || []);
  } catch(e) { /* ignore */ }
}

function renderThreadList(threads) {
  const ul = document.getElementById('thread-list');
  ul.innerHTML = '';
  for (const t of threads) {
    const li = document.createElement('li');
    li.dataset.id = t;
    const label = document.createElement('div');
    label.className = 'thread-id';
    label.textContent = t;
    li.appendChild(label);
    li.addEventListener('click', () => switchThread(t));
    if (t === currentThread) li.classList.add('active');
    ul.appendChild(li);
  }
}

// ── Thread management ─────────────────────────────────────────────────────────
function newThread() {
  const id = 'web-' + Math.random().toString(36).slice(2, 10);
  switchThread(id, true);
}

async function switchThread(threadId, isNew = false) {
  if (isStreaming || typeof threadId !== 'string') return;
  currentThread = threadId;

  // Update sidebar active state
  document.querySelectorAll('#thread-list li').forEach(li => {
    li.classList.toggle('active', li.dataset.id === threadId);
  });

  // Update header
  document.getElementById('chat-thread-label').textContent = threadId;

  // Clear messages
  clearMessages();

  // Connect WebSocket
  connect(threadId);

  // Load history (unless brand new)
  if (!isNew) {
    await loadHistory(threadId);
  }

  // Update thread list (add if new)
  const ul = document.getElementById('thread-list');
  const exists = Array.from(ul.querySelectorAll('li')).some(li => li.dataset.id === threadId);
  if (!exists) {
    const li = document.createElement('li');
    li.dataset.id = threadId;
    const label = document.createElement('div');
    label.className = 'thread-id';
    label.textContent = threadId;
    li.appendChild(label);
    li.addEventListener('click', () => switchThread(threadId));
    li.classList.add('active');
    ul.prepend(li);
  }
}

async function loadHistory(threadId) {
  try {
    const r = await fetch(`/api/threads/${encodeURIComponent(threadId)}/history`);
    const d = await r.json();
    for (const msg of d.messages || []) {
      if (msg.role === 'human') {
        addHumanBubble(msg.content, false);
      } else if (msg.role === 'ai' && msg.content) {
        addAiBubble(msg.content, false);
      } else if (msg.role === 'tool') {
        addToolChip(msg.name || 'tool', false);
      }
    }
    scrollToBottom(false);
  } catch(e) { /* ignore */ }
}

// ── WebSocket ──────────────────────────────────────────────────────────────────
function connect(threadId) {
  if (ws) { ws.onclose = null; ws.close(); ws = null; }
  setConnectionStatus('connecting');

  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws/chat/${encodeURIComponent(threadId)}`);

  ws.onopen = () => {
    setConnectionStatus('connected');
    enableInput();
  };

  ws.onclose = () => {
    setConnectionStatus('disconnected');
    disableInput();
    // Auto-reconnect after 2s if we still have a thread
    if (currentThread === threadId) {
      setTimeout(() => { if (currentThread === threadId) connect(threadId); }, 2000);
    }
  };

  ws.onerror = () => setConnectionStatus('disconnected');

  ws.onmessage = (evt) => {
    let data;
    try { data = JSON.parse(evt.data); } catch { return; }
    handleEvent(data);
  };
}

function handleEvent(data) {
  switch (data.type) {
    case 'token':
      removeTypingIndicator();
      appendToken(data.text);
      break;
    case 'tool':
      removeTypingIndicator();
      onToolCall(data.name);
      break;
    case 'usage':
      onUsage(data.usage);
      break;
    case 'interrupt':
      removeTypingIndicator();
      finishCurrentBubble();
      showInterruptModal(data.payload);
      break;
    case 'done':
      removeTypingIndicator();
      finishCurrentBubble();
      isStreaming = false;
      enableInput();
      break;
    case 'error':
      removeTypingIndicator();
      finishCurrentBubble();
      addErrorMessage(data.message);
      isStreaming = false;
      enableInput();
      break;
  }
}

// ── Sending ───────────────────────────────────────────────────────────────────
function sendMessage() {
  const ta = document.getElementById('input');
  const text = ta.value.trim();
  if (!text || isStreaming || !ws || ws.readyState !== WebSocket.OPEN) return;

  ta.value = '';
  ta.style.height = '';
  isStreaming = true;
  disableInput();

  hideEmptyState();
  addHumanBubble(text, true);
  showTypingIndicator();
  currentBubble = null;
  currentToolGroup = null;
  currentRaw = '';

  ws.send(JSON.stringify({ type: 'message', text }));
}

function quickSend(el) {
  const ta = document.getElementById('input');
  ta.value = el.textContent;
  sendMessage();
}

// ── Message rendering ─────────────────────────────────────────────────────────
function clearMessages() {
  const msgs = document.getElementById('messages');
  msgs.innerHTML = '';
  const es = document.createElement('div');
  es.id = 'empty-state';
  es.style.display = 'none'; // cleared threads show empty, not the suggestions
  msgs.appendChild(es);
  currentBubble = null;
  currentToolGroup = null;
  typingIndicatorRow = null;
  currentRaw = '';
}

function hideEmptyState() {
  const es = document.getElementById('empty-state');
  if (es) es.style.display = 'none';
}

function addHumanBubble(text, animate = true) {
  hideEmptyState();
  currentToolGroup = null;  // reset tool group for next AI message
  const row = document.createElement('div');
  row.className = 'msg-row human' + (animate ? '' : ' no-anim');
  row.innerHTML = `
    <div class="avatar human">U</div>
    <div class="bubble human">${escapeHtml(text)}</div>
  `;
  if (!animate) row.style.animation = 'none';
  document.getElementById('messages').appendChild(row);
  scrollToBottom();
  return row;
}

function addAiBubble(markdown, animate = true) {
  hideEmptyState();
  const row = document.createElement('div');
  row.className = 'msg-row ai';
  if (!animate) row.style.animation = 'none';
  const bubble = document.createElement('div');
  bubble.className = 'bubble ai';
  bubble.innerHTML = renderMarkdown(markdown);
  highlightCode(bubble);
  const meta = makeBubbleMeta(markdown);
  row.innerHTML = '<div class="avatar ai">E</div>';
  row.appendChild(bubble);
  bubble.appendChild(meta);
  document.getElementById('messages').appendChild(row);
  scrollToBottom();
  return bubble;
}

function startAiStream() {
  hideEmptyState();
  const row = document.createElement('div');
  row.className = 'msg-row ai';
  const bubble = document.createElement('div');
  bubble.className = 'bubble ai streaming';
  bubble.dataset.raw = '';
  row.innerHTML = '<div class="avatar ai">E</div>';
  row.appendChild(bubble);
  document.getElementById('messages').appendChild(row);
  currentBubble = bubble;
  scrollToBottom();
  return bubble;
}

function appendToken(text) {
  if (!currentBubble) startAiStream();
  currentRaw += text;
  currentBubble.dataset.raw = currentRaw;
  // Re-render markdown incrementally
  const rendered = renderMarkdown(currentRaw);
  // Replace content but keep the streaming class
  currentBubble.innerHTML = rendered;
  highlightCode(currentBubble);
  scrollToBottom();
}

function onToolCall(name) {
  // Finish any open AI bubble
  if (currentBubble) {
    finishCurrentBubble();
  }
  // Create/reuse tool group
  if (!currentToolGroup) {
    currentToolGroup = document.createElement('div');
    currentToolGroup.className = 'tool-group';
    document.getElementById('messages').appendChild(currentToolGroup);
  }
  const chip = document.createElement('span');
  chip.className = 'tool-chip running';
  chip.textContent = name;
  currentToolGroup.appendChild(chip);
  // Mark chip as done after a brief moment (we don't get a "tool done" event)
  setTimeout(() => chip.classList.remove('running'), 1500);
  scrollToBottom();
}

function onUsage(usage) {
  if (!currentBubble) return;
  // Store for when we finish the bubble
  currentBubble.dataset.usage = JSON.stringify(usage);
}

function finishCurrentBubble() {
  if (!currentBubble) return;
  currentBubble.classList.remove('streaming');

  // Add meta row (copy + token info) if we have content
  const raw = currentBubble.dataset.raw || currentBubble.textContent;
  if (raw.trim()) {
    const usageStr = currentBubble.dataset.usage;
    let usageHtml = '';
    if (usageStr) {
      try {
        const u = JSON.parse(usageStr);
        usageHtml = `<span>${u.input_tokens || 0}↑ ${u.output_tokens || 0}↓ tokens</span>`;
      } catch {}
    }
    const meta = document.createElement('div');
    meta.className = 'bubble-meta';
    meta.innerHTML = `
      <button class="copy-btn" onclick="copyBubble(this)">Copy</button>
      ${usageHtml}
    `;
    currentBubble.appendChild(meta);
  }

  currentBubble = null;
  currentRaw = '';
}

function addToolChip(name, animate = true) {
  if (!currentToolGroup) {
    currentToolGroup = document.createElement('div');
    currentToolGroup.className = 'tool-group';
    if (!animate) currentToolGroup.style.animation = 'none';
    document.getElementById('messages').appendChild(currentToolGroup);
  }
  const chip = document.createElement('span');
  chip.className = 'tool-chip';
  if (!animate) chip.style.animation = 'none';
  chip.textContent = name;
  currentToolGroup.appendChild(chip);
}

function makeBubbleMeta(raw) {
  const meta = document.createElement('div');
  meta.className = 'bubble-meta';
  meta.innerHTML = `<button class="copy-btn" onclick="copyBubble(this)">Copy</button>`;
  return meta;
}

function addErrorMessage(msg) {
  const row = document.createElement('div');
  row.className = 'msg-row ai';
  row.innerHTML = `
    <div class="avatar ai">!</div>
    <div class="bubble ai" style="border-color:var(--error);color:var(--error)">
      <strong>Error:</strong> ${escapeHtml(msg)}
    </div>
  `;
  document.getElementById('messages').appendChild(row);
  scrollToBottom();
}

function showTypingIndicator() {
  const row = document.createElement('div');
  row.className = 'msg-row ai';
  row.innerHTML = `
    <div class="avatar ai">E</div>
    <div class="typing-indicator">
      <span></span><span></span><span></span>
    </div>
  `;
  document.getElementById('messages').appendChild(row);
  typingIndicatorRow = row;
  scrollToBottom();
}

function removeTypingIndicator() {
  if (typingIndicatorRow) {
    typingIndicatorRow.remove();
    typingIndicatorRow = null;
  }
}

// ── Interrupt modal ───────────────────────────────────────────────────────────
function showInterruptModal(payload) {
  disableInput();
  document.getElementById('interrupt-action').textContent =
    `Action: ${payload.action || ''}`;
  document.getElementById('interrupt-summary').textContent =
    payload.summary || 'Are you sure you want to proceed?';
  document.getElementById('interrupt-overlay').classList.add('visible');
}

function replyInterrupt(confirmed) {
  document.getElementById('interrupt-overlay').classList.remove('visible');
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  const answer = confirmed ? 'yes' : 'no';
  isStreaming = true;
  showTypingIndicator();
  currentBubble = null;
  currentRaw = '';
  ws.send(JSON.stringify({ type: 'resume', answer }));
}

// ── Utilities ─────────────────────────────────────────────────────────────────
function renderMarkdown(text) {
  try {
    return sanitizeHtml(marked.parse(text, { breaks: true, gfm: true }));
  } catch {
    return escapeHtml(text);
  }
}

function sanitizeHtml(html) {
  const template = document.createElement('template');
  template.innerHTML = html;
  template.content.querySelectorAll('script,iframe,object,embed,link,meta,style').forEach(
    element => element.remove()
  );
  template.content.querySelectorAll('*').forEach(element => {
    for (const attr of Array.from(element.attributes)) {
      const name = attr.name.toLowerCase();
      const value = attr.value.trim().toLowerCase();
      if (name.startsWith('on') || name === 'srcdoc' || name === 'style') {
        element.removeAttribute(attr.name);
      } else if ((name === 'href' || name === 'src') &&
                 !/^(https?:|mailto:|#)/.test(value) && !value.startsWith('/')) {
        element.removeAttribute(attr.name);
      }
    }
  });
  return template.innerHTML;
}

function highlightCode(el) {
  el.querySelectorAll('pre code').forEach(block => {
    try { hljs.highlightElement(block); } catch {}
  });
}

function escapeHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
          .replace(/"/g,'&quot;').replace(/'/g,'&#039;');
}

function copyBubble(btn) {
  const bubble = btn.closest('.bubble');
  const raw = bubble.dataset.raw || bubble.innerText;
  navigator.clipboard.writeText(raw).then(() => {
    btn.textContent = 'Copied!';
    btn.classList.add('copied');
    setTimeout(() => { btn.textContent = 'Copy'; btn.classList.remove('copied'); }, 1500);
  });
}

function scrollToBottom(smooth = true) {
  const msgs = document.getElementById('messages');
  if (smooth) {
    msgs.scrollTo({ top: msgs.scrollHeight, behavior: 'smooth' });
  } else {
    msgs.scrollTop = msgs.scrollHeight;
  }
}

function setConnectionStatus(state) {
  const dot = document.getElementById('connection-dot');
  dot.className = '';
  if (state === 'connected') {
    dot.classList.add('connected');
    dot.title = 'Connected';
  } else if (state === 'connecting') {
    dot.classList.add('connecting');
    dot.title = 'Connecting...';
  } else {
    dot.title = 'Disconnected — retrying...';
  }
}

function enableInput() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  document.getElementById('input').disabled = false;
  document.getElementById('send-btn').disabled = false;
  document.getElementById('input').focus();
}

function disableInput() {
  document.getElementById('send-btn').disabled = true;
}

function setupInput() {
  const ta = document.getElementById('input');

  // Auto-resize textarea
  ta.addEventListener('input', () => {
    ta.style.height = 'auto';
    ta.style.height = Math.min(ta.scrollHeight, 160) + 'px';
  });

  // Enter to send, Shift+Enter for newline
  ta.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  // Enable send button when there's text
  ta.addEventListener('input', () => {
    const hasTxt = ta.value.trim().length > 0;
    const btn = document.getElementById('send-btn');
    if (!isStreaming && ws && ws.readyState === WebSocket.OPEN) {
      btn.disabled = !hasTxt;
    }
  });
}

// Start with a new thread on first load
newThread();
</script>
</body>
</html>
"""
