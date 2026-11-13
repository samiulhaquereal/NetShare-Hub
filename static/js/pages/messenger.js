/* NetShare Hub — messenger page. Globals from common.js: showToast, escapeHtml; config: window.NSH */
const STREAM_WS_URL = window.NSH.wsUrl;
const WS_URL = STREAM_WS_URL.replace("/ws/stream", "/ws/chat");
let ws;
let myUsername = "";
let myMacId = "";
let currentTarget = "group";
let unreadCounts = {};

let allUsers = [];
let myGroups = [];
let chatPreviews = {};
let myAvatar = "";
let typingUsers = {}; // { targetId: { userId: timeout } }

// Pagination States
let sidebarPage = 0;
let sidebarHasMore = true;
let sidebarLoading = false;
let firstMsgId = null;
let chatLoading = false;
let chatHasMore = true;
let mediaRecorder;
let audioChunks = [];
let isRecording = false;

window.onload = async () => {
  const pinOk = await checkPinStatus();
  if (!pinOk) return;

  // Register PWA Service Worker
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(e => console.error('SW Error', e));
  }

  try {
    const res = await fetch('/api/user');
    const data = await res.json();
    if (data.username) {
      myUsername = data.username;
      myMacId = data.mac;
      myAvatar = data.avatar;
      completeLogin();
    } else {
      document.getElementById('username-input').focus();
    }
  } catch (e) {
    document.getElementById('username-input').focus();
  }
};

function handleEnter(e, cb) { if (e.key === 'Enter') cb(); }

function editProfile() {
  document.getElementById('modal-title').textContent = "Update Profile Name";
  document.getElementById('modal-btn').textContent = "Save Changes";
  document.getElementById('username-input').value = myUsername;
  document.getElementById('username-modal').style.display = 'flex';
  document.getElementById('username-input').focus();
}

async function joinApp() {
  const val = document.getElementById('username-input').value.trim();
  if (!val) return;

  const res = await fetch('/api/user', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: val })
  });
  const data = await res.json();
  myUsername = data.username;
  myMacId = data.mac;
  completeLogin();
}

async function completeLogin() {
  document.getElementById('username-modal').style.display = 'none';
  document.getElementById('my-profile-name').innerHTML = `<code>${escapeHtml(myUsername)}</code>`;
  document.getElementById('chat-input').disabled = false;
  document.getElementById('send-btn').disabled = false;

  renderAvatarPreview();
  await fetchAllUsers();
  await fetchSidebar();
  await fetchMessages();
  if (!ws || ws.readyState !== WebSocket.OPEN) { connectWebSocket(); }
}

function renderAvatarPreview() {
  const preview = document.getElementById('profile-avatar-preview');
  if (myAvatar) {
    preview.innerHTML = `<img src="${escapeHtml(myAvatar)}" style="width:100%; height:100%; object-fit:cover;">`;
  } else {
    preview.textContent = myUsername ? myUsername[0].toUpperCase() : "?";
  }
}

function getAvatarHtml(avatarUrl, name, size = 40, isActive = false) {
  const initials = escapeHtml(name ? name[0].toUpperCase() : "?");
  avatarUrl = avatarUrl ? escapeHtml(avatarUrl) : avatarUrl;
  const statusColor = isActive ? '#10B981' : '#64748B';
  const activeDot = `<span class="active-dot" style="position:absolute; bottom:2px; right:2px; border:2px solid var(--sidebar-bg); background:${statusColor}; margin:0; width:10px; height:10px;"></span>`;

  if (avatarUrl) {
    return `<div style="position:relative; width:${size}px; height:${size}px; border-radius:50%; flex-shrink:0; margin-right:12px;">
                <img src="${avatarUrl}" style="width:100%; height:100%; border-radius:50%; object-fit:cover;">
                ${activeDot}
            </div>`;
  }
  return `<div style="position:relative; width:${size}px; height:${size}px; border-radius:50%; background:var(--primary); color:white; display:flex; align-items:center; justify-content:center; font-weight:600; flex-shrink:0; margin-right:12px;">
            ${initials}
            ${activeDot}
        </div>`;
}

function updateTypingUI(tid) {
  const users = Object.keys(typingUsers[tid] || {});
  const previewId = `preview-${tid}`;
  const previewEl = document.getElementById(previewId);
  if (!previewEl) return;

  if (users.length > 0) {
    let text = "";
    if (tid === 'group' || tid.startsWith('cg_')) {
      text = users.length === 1 ? "Someone is typing..." : "Several people are typing...";
    } else {
      text = "is typing...";
    }
    previewEl.innerHTML = `<span style="color:var(--primary); font-style:italic;">${text}</span>`;
  } else {
    // Restore original preview
    const prev = chatPreviews[tid] ? (chatPreviews[tid].text || "") : "No messages yet";
    previewEl.textContent = prev.length > 35 ? prev.substring(0, 35) + "..." : prev;
  }
}

async function fetchAllUsers() {
  try {
    const res = await fetch('/api/users');
    allUsers = await res.json();
  } catch (e) { console.error("Failed to fetch users", e); }
}

function createNavItem(id, iconHtml, title, isGroup, isPinned = false) {
  const div = document.createElement('div');
  div.className = `chat-item chat-item-user ${currentTarget === id ? 'active' : ''}`;
  if (id === 'group') div.className = `chat-item ${currentTarget === 'group' ? 'active' : ''}`;

  div.id = `item-${id}`;
  div.onclick = (e) => { if (!e.target.closest('.pin-btn')) switchChat(id, title); };

  const badgeCount = unreadCounts[id] || 0;
  const extraStyles = (id === 'group') ? 'color:var(--primary); font-weight:600;' : (isGroup ? 'font-weight:600;' : '');

  let avatarArea = iconHtml;
  if (id !== 'group' && !isGroup) {
    const u = allUsers.find(x => x.mac === id);
    avatarArea = getAvatarHtml(u ? u.avatar : null, title, 40, u ? u.is_active : false);
  }

  let previewText = chatPreviews[id] ? (chatPreviews[id].text || "") : "No messages yet";
  const safePreview = escapeHtml(previewText.length > 35 ? previewText.substring(0, 35) + "..." : previewText);
  const safeTitle = escapeHtml(title);

  div.innerHTML = `
            ${avatarArea}
            <div style="flex:1; overflow:hidden;">
                <div class="chat-item-title" style="display:flex; align-items:center; ${extraStyles}">
                    <span class="uname-text" style="white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${safeTitle}</span>
                    ${isPinned ? '<span style="margin-left:4px; font-size:0.75rem; opacity:0.8;">📌</span>' : ''}
                </div>
                <div id="preview-${id}" style="font-size:0.75rem; color:var(--text-muted); margin-top:6px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">
                    ${safePreview}
                </div>
            </div>
            <div style="display:flex; flex-direction:column; align-items:flex-end; gap:5px;">
               <div class="unread-badge" id="badge-${id}" style="display: ${badgeCount ? 'block' : 'none'};">${badgeCount}</div>
               <button class="pin-btn" onclick="togglePin('${id}', ${isPinned})" style="background:none; border:none; color:var(--text-muted); padding:2px; font-size:0.9rem; opacity:0.3; transition:0.2s;" title="${isPinned ? 'Unpin' : 'Pin'}">
                    ${isPinned ? '📌' : '📍'}
               </button>
            </div>
        `;

  div.onmouseover = () => div.querySelector('.pin-btn').style.opacity = '1';
  div.onmouseout = () => { if (!isPinned) div.querySelector('.pin-btn').style.opacity = '0.3'; };
  if (isPinned) div.querySelector('.pin-btn').style.opacity = '1';

  return div;
}

async function fetchSidebar(reset = true) {
  if (sidebarLoading) return;
  if (reset) {
    sidebarPage = 0;
    sidebarHasMore = true;
    const listEl = document.getElementById('chat-list-el');
    listEl.innerHTML = '';
  }
  if (!sidebarHasMore) return;

  sidebarLoading = true;
  const res = await fetch(`/api/sidebar?page=${sidebarPage}&limit=20`);
  const data = await res.json();
  sidebarLoading = false;

  chatPreviews = { ...chatPreviews, ...data.previews };
  sidebarHasMore = data.has_more;
  sidebarPage++;

  const listEl = document.getElementById('chat-list-el');
  data.items.forEach(item => {
    // Adjust icon html based on type
    let iconHtml = '';
    if (item.type === 'global') iconHtml = '🌟&nbsp;&nbsp;';
    else if (item.type === 'cgroup') iconHtml = '<span style="font-size:1.1rem; margin-right:6px;">👥</span>';
    else {
      const dotColor = item.is_active ? '#10B981' : '#64748B';
      iconHtml = `<span class="active-dot" style="background:${dotColor}"></span>`;
    }

    // Add to list if not already there (WebSocket might have added it)
    if (!document.getElementById(`item-${item.id}`)) {
      listEl.appendChild(createNavItem(item.id, iconHtml, item.title, item.type === 'cgroup', item.is_pinned));
    }
  });

  filterUsers();
}

// Sidebar scroll listener
document.getElementById('chat-list-el').onscroll = function () {
  if (this.scrollTop + this.clientHeight >= this.scrollHeight - 50) {
    fetchSidebar(false);
  }
};

function filterUsers() {
  const query = document.getElementById('user-search').value.toLowerCase();
  const items = document.querySelectorAll('.chat-item-user');
  items.forEach(el => {
    const text = el.querySelector('.uname-text').textContent.toLowerCase();
    el.style.display = text.includes(query) ? 'flex' : 'none';
  });
}

// --- Group Management Core Logic ---
function openGroupModal() {
  const list = document.getElementById('group-user-list');
  list.innerHTML = '';
  allUsers.forEach(u => {
    if (u.mac === myMacId) return;
    list.innerHTML += `
                <label style="display:flex; align-items:center; gap:10px; cursor:pointer; padding:6px; border-radius:6px; background:rgba(255,255,255,0.02)">
                    <input type="checkbox" class="group-member-checkbox" value="${escapeHtml(u.mac)}">
                    <span style="flex:1;">${escapeHtml(u.username)}</span>
                    <span class="active-dot" style="background:${u.is_active ? '#10B981' : '#64748B'}; margin:0;"></span>
                </label>
            `;
  });
  document.getElementById('group-name-input').value = '';
  document.getElementById('group-modal').style.display = 'flex';
}

async function submitGroup() {
  const name = document.getElementById('group-name-input').value.trim();
  if (!name) return alert('Enter a group name!');

  const checks = document.querySelectorAll('.group-member-checkbox:checked');
  const macs = Array.from(checks).map(c => c.value);
  if (macs.length === 0) return alert('Select at least one member!');

  const res = await fetch('/api/groups', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: name, members: macs })
  });

  if (res.ok) {
    document.getElementById('group-modal').style.display = 'none';
    const data = await res.json();
    await fetchSidebar();
    switchChat(data.id, name);
  } else {
    alert('Failed to generate group.');
  }
}

async function openEditGroupModal(groupId) {
  const groupObj = myGroups.find(g => g.id === groupId);
  if (!groupObj) return;

  const res = await fetch(`/api/groups/${groupId}/members`);
  const currentMembers = await res.json();

  const list = document.getElementById('edit-group-user-list');
  list.innerHTML = '';

  allUsers.forEach(u => {
    if (u.mac === myMacId) return;
    const checked = currentMembers.includes(u.mac) ? 'checked' : '';
    list.innerHTML += `
                <label style="display:flex; align-items:center; gap:10px; cursor:pointer; padding:6px; border-radius:6px; background:rgba(255,255,255,0.02)">
                    <input type="checkbox" class="edit-group-member-checkbox" value="${escapeHtml(u.mac)}" ${checked}>
                    <span style="flex:1;">${escapeHtml(u.username)}</span>
                    <span class="active-dot" style="background:${u.is_active ? '#10B981' : '#64748B'}; margin:0;"></span>
                </label>
            `;
  });
  document.getElementById('edit-group-name-input').value = groupObj.name;
  document.getElementById('edit-group-id-hidden').value = groupId;
  document.getElementById('edit-group-modal').style.display = 'flex';
}

async function submitEditGroup() {
  const name = document.getElementById('edit-group-name-input').value.trim();
  const groupId = document.getElementById('edit-group-id-hidden').value;
  if (!name) return alert('Enter a group name!');

  const checks = document.querySelectorAll('.edit-group-member-checkbox:checked');
  const macs = Array.from(checks).map(c => c.value);
  if (macs.length === 0) return alert('Keep at least one member inside.');

  const res = await fetch(`/api/groups/${groupId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: name, members: macs })
  });

  if (res.ok) {
    document.getElementById('edit-group-modal').style.display = 'none';
    await fetchSidebar();
    switchChat(groupId, name);
  } else {
    alert('Connection failure: Unable to update remote group.');
  }
}

async function openViewMembersModal(groupId) {
  const groupObj = myGroups.find(g => g.id === groupId);
  if (!groupObj) return;

  const res = await fetch(`/api/groups/${groupId}/members`);
  const memberMacs = await res.json();

  const list = document.getElementById('view-members-list');
  list.innerHTML = '';

  // Generate list matching actual known users
  memberMacs.forEach(mac => {
    let u = allUsers.find(x => x.mac === mac);
    let username = u ? u.username : "Unknown Entity";
    let isActive = u ? u.is_active : false;

    if (mac === myMacId) {
      username = myUsername + " (You)";
      isActive = true;
    }

    let adminBadge = (mac === groupObj.created_by)
      ? '<span style="font-size:0.65rem; background:var(--primary); color:white; padding:2px 8px; border-radius:10px; margin-left:10px;">Admin</span>'
      : '';

    list.innerHTML += `
                <div style="display:flex; align-items:center; gap:10px; padding:8px 10px; border-bottom:1px solid rgba(255,255,255,0.05); background:rgba(255,255,255,0.02); border-radius:6px;">
                    <span style="flex:1;">${escapeHtml(username)} ${adminBadge}</span>
                    <span class="active-dot" style="background:${isActive ? '#10B981' : '#64748B'}; margin:0;"></span>
                </div>
            `;
  });
  document.getElementById('view-members-modal').style.display = 'flex';
}
// -------------------------------

async function switchChat(targetId, title) {
  currentTarget = targetId;
  document.getElementById('chat-title').textContent = title;
  chatHasMore = true;
  firstMsgId = null;

  let toolsHtml = '';
  // Fetch myGroups if needed or use from sidebar data (we should ideally have a global myGroups cache)
  // For now, let's just make it work with the existing flow.
  const gRes = await fetch('/api/groups');
  myGroups = await gRes.json();

  if (targetId.startsWith('cg_')) {
    const groupObj = myGroups.find(g => g.id === targetId);

    // Visible to EVERYONE in the group: The "View Members" list icon
    toolsHtml += `
                <button onclick="openViewMembersModal('${targetId}')" class="icon-btn" title="View Members" style="width:36px; height:36px; border:1px solid var(--border-color); color:var(--text); margin-right:8px;">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"></path><circle cx="9" cy="7" r="4"></circle><path d="M23 21v-2a4 4 0 0 0-3-3.87"></path><path d="M16 3.13a4 4 0 0 1 0 7.75"></path></svg>
                </button>
            `;

    // EXCLUSIVE ADMIN ACCESS VERIFICATION!
    if (groupObj && groupObj.created_by === myMacId) {
      toolsHtml += `
                    <button onclick="openEditGroupModal('${targetId}')" class="icon-btn" title="Edit Group Setting" style="width:36px; height:36px; border:1px solid var(--border-color); color:var(--text);">
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"></circle><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"></path></svg>
                    </button>
                `;
    }
  }
  document.getElementById('chat-header-tools').innerHTML = toolsHtml;

  unreadCounts[targetId] = 0;
  const b = document.getElementById(`badge-${targetId}`);
  if (b) b.style.display = 'none';

  document.querySelectorAll('.chat-item').forEach(el => el.classList.remove('active'));
  const activeEl = document.getElementById(`item-${targetId}`);
  if (activeEl) activeEl.classList.add('active');

  if (window.innerWidth <= 768) toggleSidebar();

  await fetchMessages(true);
}

async function fetchMessages(clear = false) {
  if (chatLoading || (!chatHasMore && !clear)) return;

  const container = document.getElementById('chat-container');
  const oldScrollHeight = container.scrollHeight;

  if (clear) {
    container.innerHTML = '<div style="text-align:center; color: var(--text-muted)">Loading...</div>';
    firstMsgId = null;
    chatHasMore = true;
  }

  chatLoading = true;
  const limit = 20;
  const url = `/api/messages?target=${currentTarget}&limit=${limit}${firstMsgId ? `&before_id=${firstMsgId}` : ''}`;
  const res = await fetch(url);
  const messages = await res.json();
  chatLoading = false;

  if (clear) container.innerHTML = '';

  // Remove existing load more button if any
  const oldBtn = document.getElementById('load-more-history');
  if (oldBtn) oldBtn.remove();

  if (messages.length < limit) {
    chatHasMore = false;
  }

  if (messages.length > 0) {
    firstMsgId = messages[0].id;

    const messagesFragment = document.createDocumentFragment();
    messages.forEach(m => messagesFragment.appendChild(createMessageElement(m)));

    container.prepend(messagesFragment);

    if (clear) {
      container.scrollTop = container.scrollHeight;
    } else {
      container.scrollTop = container.scrollHeight - oldScrollHeight;
    }
  }

  // If there's more history, add the button at the very top
  if (chatHasMore) {
    const loadMoreBtn = document.createElement('button');
    loadMoreBtn.id = 'load-more-history';
    loadMoreBtn.className = 'load-more-btn';
    loadMoreBtn.textContent = 'Load older messages';
    loadMoreBtn.onclick = () => fetchMessages(false);
    container.prepend(loadMoreBtn);
  }

  sendReadReceipt(currentTarget);
}

// Chat container scroll listener for history
document.getElementById('chat-container').onscroll = function () {
  if (this.scrollTop === 0 && chatHasMore) {
    fetchMessages(false);
  }
};

function toggleSidebar() {
  const s = document.getElementById('sidebar');
  s.classList.toggle('closed');
}

function connectWebSocket() {
  let wsTarget = WS_URL;
  const pinToken = localStorage.getItem("pin_token");
  if (pinToken) {
    wsTarget += (wsTarget.includes('?') ? '&' : '?') + 'pin_token=' + encodeURIComponent(pinToken);
  }
  ws = new WebSocket(wsTarget);
  ws.onopen = () => {
    document.getElementById('connection-status').style.background = '#10B981';
    document.getElementById('connection-status').title = 'Connected';
  };
  ws.onclose = () => {
    document.getElementById('connection-status').style.background = '#EF4444';
    document.getElementById('connection-status').title = 'Disconnected';
    setTimeout(connectWebSocket, 3000);
  };
  ws.onmessage = async (event) => {
    const data = JSON.parse(event.data);

    if (data.type === 'system' && (data.action === 'update_users' || data.action === 'update_groups')) {
      if (data.action === 'update_users') await fetchAllUsers();
      await fetchSidebar();
      // If the current group just got deleted/I got kicked, jump to global chat
      if (currentTarget.startsWith('cg_')) {
        if (!myGroups.find(g => g.id === currentTarget)) {
          switchChat('group', 'Group Chat');
        } else {
          // Automatically update the header text in case Admin renamed the group dynamically
          const activeG = myGroups.find(g => g.id === currentTarget);
          if (document.getElementById('chat-title').textContent !== activeG.name) {
            document.getElementById('chat-title').textContent = activeG.name;
          }
        }
      }
      return;
    }

    if (data.type === 'update_msg') {
      const oldMsg = document.getElementById(`msg-container-${data.msg.id}`);
      if (oldMsg) {
        oldMsg.replaceWith(createMessageElement(data.msg));

        // Live Sidebar Sync for Edit/Delete
        let msgContext = "group";
        if (data.msg.to_target !== "group") {
          if (data.msg.to_target.startsWith("cg_")) msgContext = data.msg.to_target;
          else msgContext = data.msg.from_mac === myMacId ? data.msg.to_target : data.msg.from_mac;
        }
        const newTxt = data.msg.is_deleted ? "🚫 Deleted" : (data.msg.msg_type === 'file' ? "📎 [File]" : (data.msg.content || ""));

        if (!chatPreviews[msgContext]) chatPreviews[msgContext] = { time_id: data.msg.id };
        chatPreviews[msgContext].text = newTxt;

        const el = document.getElementById(`preview-${msgContext}`);
        if (el) {
          el.textContent = newTxt.length > 35 ? newTxt.substring(0, 35) + "..." : newTxt;
        }
        return;
      }
    }

    if (data.type === 'read_receipt') {
      if (data.reader === currentTarget) {
        document.querySelectorAll('.msg-mine .tick-sent').forEach(el => {
          el.classList.remove('tick-sent');
          el.classList.add('tick-read');
        });
      }
      return;
    }
    if (data.type === 'typing') {
      const tid = data.target;
      const fromId = data.from;
      if (!typingUsers[tid]) typingUsers[tid] = {};
      if (data.is_typing) {
        if (typingUsers[tid][fromId]) clearTimeout(typingUsers[tid][fromId]);
        typingUsers[tid][fromId] = setTimeout(() => {
          delete typingUsers[tid][fromId];
          updateTypingUI(tid);
        }, 3000);
      } else {
        delete typingUsers[tid][fromId];
      }
      updateTypingUI(tid);
      return;
    }

    // Routing Logic
    let msgContext = "group";
    const msgToTarget = data.msg.to_target || "group";
    const msgFromMac = data.msg.from_mac || "";

    if (msgToTarget !== "group") {
      if (msgToTarget.startsWith("cg_")) {
        msgContext = msgToTarget;
      } else {
        // Normalize IDs to prevent comparison failures
        msgContext = (msgFromMac.trim().toLowerCase() === myMacId.trim().toLowerCase()) ? msgToTarget : msgFromMac;
      }
    }

    // Live preview mapping
    const previewTxt = data.msg.msg_type === 'file' ? "📎 [File]" : (data.msg.content || "");
    chatPreviews[msgContext] = { text: previewTxt, time_id: data.msg.id };

    const pel = document.getElementById(`preview-${msgContext}`);
    if (pel) pel.textContent = previewTxt.length > 35 ? previewTxt.substring(0, 35) + "..." : previewTxt;

    // DYNAMIC BUMP TO TOP
    let navItem = document.getElementById(`item-${msgContext}`);
    const listEl = document.getElementById('chat-list-el');

    if (!navItem) {
      let title = "User";
      let iconHtml = "";
      let isGroup = msgContext.startsWith("cg_");

      if (msgContext === 'group') {
        title = "Global Chat";
        iconHtml = "🌟&nbsp;&nbsp;";
      } else if (isGroup) {
        const g = myGroups.find(x => x.id === msgContext);
        title = g ? g.name : "Group";
        iconHtml = '<span style="font-size:1.1rem; margin-right:6px;">👥</span>';
      } else {
        const u = allUsers.find(x => x.mac === msgContext);
        title = u ? u.username : "User";
        const dotColor = (u && u.is_active) ? '#10B981' : '#64748B';
        iconHtml = `<span class="active-dot" style="background:${dotColor}"></span>`;
      }
      navItem = createNavItem(msgContext, iconHtml, title, isGroup);
    }
    listEl.insertBefore(navItem, listEl.firstChild);

    if (msgContext.trim().toLowerCase() === currentTarget.trim().toLowerCase()) {
      const container = document.getElementById('chat-container');
      const msgEl = createMessageElement(data.msg);
      container.appendChild(msgEl);
      container.scrollTop = container.scrollHeight;

      if (msgFromMac.trim().toLowerCase() !== myMacId.trim().toLowerCase()) {
        sendReadReceipt(msgFromMac);
      }
    } else {
      unreadCounts[msgContext] = (unreadCounts[msgContext] || 0) + 1;
      const b = document.getElementById(`badge-${msgContext}`);
      if (b) {
        b.textContent = unreadCounts[msgContext];
        b.style.display = 'block';
      }
    }
  };
}

function formatTime(isoStr, isEdited) {
  if (!isoStr) return "";
  const safeStr = isoStr.replace(' ', 'T') + 'Z';
  const d = new Date(safeStr);
  let timestring = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  if (isEdited) timestring += " (edited)";
  return timestring;
}

function sendReadReceipt(senderMac) {
  if (senderMac !== 'group' && !senderMac.startsWith('cg_') && ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'read', to_target: senderMac }));
  }
}

let typingTimeout;
document.getElementById('chat-input').oninput = function () {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  if (typingTimeout) clearTimeout(typingTimeout);

  ws.send(JSON.stringify({ type: 'typing', target: currentTarget, is_typing: true }));

  typingTimeout = setTimeout(() => {
    ws.send(JSON.stringify({ type: 'typing', target: currentTarget, is_typing: false }));
  }, 2000);
};

async function sendTextMessage() {
  const input = document.getElementById('chat-input');
  const text = input.value.trim();
  if (!text || !ws) return;

  ws.send(JSON.stringify({ type: 'text', to_target: currentTarget, text: text }));
  input.value = '';

  // Immediately send "stop typing" when message is sent
  if (typingTimeout) clearTimeout(typingTimeout);
  ws.send(JSON.stringify({ type: 'typing', target: currentTarget, is_typing: false }));
}

async function uploadFile() {
  const input = document.getElementById('file-input');
  const file = input.files[0];
  if (!file) return;

  document.getElementById('upload-progress').style.display = 'block';
  let formData = new FormData();
  formData.append('file', file);

  const res = await fetch('/chat/upload', { method: 'POST', body: formData });
  const result = await res.json();
  document.getElementById('upload-progress').style.display = 'none';

  if (result.status === 'ok') {
    ws.send(JSON.stringify({
      type: 'file', to_target: currentTarget,
      filename: result.filename, url: result.url, filetype: file.type
    }));
  }
  input.value = '';
}

function guiEditMessage(id, oldText) {
  const newText = prompt("Edit message:", oldText);
  if (newText && newText !== oldText) {
    ws.send(JSON.stringify({ type: 'edit', msg_id: id, text: newText }));
  }
}
function guiDeleteMessage(id) {
  if (confirm("Are you sure you want to delete this message instantly for everyone?")) {
    ws.send(JSON.stringify({ type: 'delete', msg_id: id }));
  }
}

function createMessageElement(data) {
  const isMine = data.from_mac === myMacId;
  const msgDiv = document.createElement('div');
  msgDiv.className = `message ${isMine ? 'msg-mine' : 'msg-other'}`;
  msgDiv.id = `msg-container-${data.id}`;

  // Add Avatar to the bubble
  const avatarWrapper = document.createElement('div');
  avatarWrapper.style.display = 'flex';
  avatarWrapper.style.alignItems = 'flex-end';
  avatarWrapper.style.gap = '8px';
  if (isMine) avatarWrapper.style.flexDirection = 'row-reverse';

  const u = allUsers.find(x => x.mac === data.from_mac);
  const avatarHtml = getAvatarHtml(u ? u.avatar : null, data.sender_name || '?', 32);
  const avDiv = document.createElement('div');
  avDiv.innerHTML = avatarHtml;
  avatarWrapper.appendChild(avDiv);

  const contentStack = document.createElement('div');
  contentStack.style.flex = '1';
  contentStack.style.display = 'flex';
  contentStack.style.flexDirection = 'column';

  const senderDiv = document.createElement('div');
  senderDiv.className = 'msg-sender';
  senderDiv.textContent = isMine ? 'You' : (data.sender_name || 'Unknown');
  contentStack.appendChild(senderDiv);

  const wrapperDiv = document.createElement('div');
  wrapperDiv.className = 'msg-bubble-wrapper';

  const bubbleDiv = document.createElement('div');
  bubbleDiv.className = 'msg-bubble';
  if (data.is_deleted) bubbleDiv.classList.add('deleted-msg');

  if (!data.is_deleted) {
    const options = document.createElement('div');
    options.className = 'msg-options';
    // Build buttons as DOM nodes: message text must never be spliced into inline JS.
    const addOption = (icon, title, handler) => {
      const btn = document.createElement('button');
      btn.className = 'msg-option-btn';
      btn.title = title;
      btn.textContent = icon;
      btn.addEventListener('click', handler);
      options.appendChild(btn);
    };
    if (!isMine) {
      addOption('😊', 'React', (ev) => showReactionPicker(data.id, ev.currentTarget));
    } else {
      if (data.msg_type === 'text') addOption('✏️', 'Edit text', () => guiEditMessage(data.id, data.content || ''));
      addOption('🗑️', 'Delete entire message', () => guiDeleteMessage(data.id));
    }
    wrapperDiv.appendChild(options);
  }

  if (data.msg_type === 'text' || data.is_deleted) {
    let renderText = escapeHtml(data.content || "");
    if (data.is_deleted) renderText = renderText.replace(/_([^_]+)_/g, '<i>$1</i>');
    bubbleDiv.innerHTML = renderText;
  } else if (data.msg_type === 'file') {
    const fileUri = data.file_url || data.url; // Support legacy and new property names
    const hasUrl = !!(fileUri && fileUri !== 'null' && fileUri !== 'undefined');

    if (data.filetype && data.filetype.startsWith('image/')) {
      if (!hasUrl) {
        bubbleDiv.innerHTML = `<div class="attachment-error">⚠️ Image unavailable</div>`;
      } else {
        const img = document.createElement('img');
        img.src = fileUri;
        img.className = 'attachment-img';
        img.onclick = () => window.open(fileUri, '_blank');
        bubbleDiv.appendChild(img);
      }
    } else if (data.filetype && data.filetype.startsWith('audio/')) {
      if (!hasUrl) {
        bubbleDiv.innerHTML = `<div class="attachment-error">⚠️ Audio unavailable</div>`;
      } else {
        const audio = document.createElement('audio');
        audio.controls = true;
        audio.src = fileUri;
        audio.style.maxWidth = '100%';
        audio.style.marginTop = '5px';
        bubbleDiv.appendChild(audio);
      }
    } else {
      if (!hasUrl) {
        bubbleDiv.innerHTML = `
                    <div class="attachment-file disabled">
                        <svg width="24" height="24" fill="none" stroke="currentColor" stroke-width="2"><path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"></path><polyline points="13 2 13 9 20 9"></polyline></svg>
                        <span>${escapeHtml(data.filename || 'File unavailable')}</span>
                    </div>`;
      } else {
        bubbleDiv.innerHTML = `
                    <a href="${escapeHtml(fileUri)}" class="attachment-file" target="_blank" rel="noopener">
                        <svg width="24" height="24" fill="none" stroke="currentColor" stroke-width="2"><path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"></path><polyline points="13 2 13 9 20 9"></polyline></svg>
                        <span>${escapeHtml(data.filename)}</span>
                    </a>`;
      }
    }
  }

  let ticksHtml = '';
  if (isMine && data.to_target !== 'group' && !data.to_target.startsWith('cg_')) {
    const tickClass = data.status === 'read' ? 'tick-read' : 'tick-sent';
    ticksHtml = `
                <span class="msg-status ${tickClass}" style="margin-left:5px; vertical-align:middle;">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M18 6L7 17l-5-5"></path><path d="M22 10l-7.5 7.5L13 16"></path>
                    </svg>
                </span>
            `;
  }

  const reactionsDiv = document.createElement('div');
  reactionsDiv.className = 'msg-reactions';
  if (data.reactions) {
    Object.entries(data.reactions).forEach(([emoji, macs]) => {
      const chip = document.createElement('div');
      const isMine = macs.includes(myMacId);
      chip.className = `reaction-chip ${isMine ? 'mine' : ''}`;
      chip.innerHTML = `<span>${escapeHtml(emoji)}</span> <span style="opacity:0.7">${macs.length}</span>`;
      chip.onclick = () => toggleReaction(data.id, emoji, isMine);
      reactionsDiv.appendChild(chip);
    });
  }
  bubbleDiv.appendChild(reactionsDiv);

  const timeSpan = document.createElement('span');
  timeSpan.className = 'msg-time';
  timeSpan.innerHTML = formatTime(data.timestamp, data.is_edited) + ticksHtml;

  bubbleDiv.appendChild(timeSpan);
  wrapperDiv.appendChild(bubbleDiv);
  contentStack.appendChild(wrapperDiv);
  avatarWrapper.appendChild(contentStack);
  msgDiv.appendChild(avatarWrapper);

  return msgDiv;
}
async function toggleReaction(msgId, emoji, currentlyMine) {
  const method = currentlyMine ? 'DELETE' : 'POST';
  await fetch(`/api/messages/${msgId}/react`, {
    method: method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ emoji: emoji })
  });
}

function showReactionPicker(msgId, btn) {
  const emojis = ['👍', '❤️', '😂', '😮', '😢', '🔥'];
  const picker = document.createElement('div');
  picker.style.position = 'fixed';
  picker.style.background = 'var(--sidebar-bg)';
  picker.style.border = '1px solid var(--border-color)';
  picker.style.borderRadius = '20px';
  picker.style.padding = '5px 10px';
  picker.style.display = 'flex';
  picker.style.gap = '8px';
  picker.style.zIndex = '1000';
  picker.style.boxShadow = '0 4px 15px rgba(0,0,0,0.3)';

  const rect = btn.getBoundingClientRect();
  picker.style.top = (rect.top - 45) + 'px';
  picker.style.left = rect.left + 'px';

  emojis.forEach(e => {
    const span = document.createElement('span');
    span.textContent = e;
    span.style.cursor = 'pointer';
    span.style.fontSize = '1.2rem';
    span.onclick = () => {
      toggleReaction(msgId, e, false);
      document.body.removeChild(picker);
    };
    picker.appendChild(span);
  });

  document.body.appendChild(picker);
  setTimeout(() => {
    const closer = () => { if (picker.parentNode) document.body.removeChild(picker); document.removeEventListener('click', closer); };
    document.addEventListener('click', closer);
  }, 10);
}

async function toggleVoiceRecording() {
  if (!isRecording) {
    // Browser security rules usually require HTTPS or localhost for mic access
    if (!window.isSecureContext && window.location.hostname !== 'localhost' && !window.location.hostname.startsWith('127.')) {
      console.warn("⚠️ Page not in a secure context. Microphone access might be blocked by the browser.");
      // We'll continue anyway and let navigator.mediaDevices.getUserMedia fail if it must,
      // rather than hard-blocking it here, as some local environments or browser flags might allow it.
    }

    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      mediaRecorder = new MediaRecorder(stream);
      audioChunks = [];
      mediaRecorder.ondataavailable = e => audioChunks.push(e.data);
      mediaRecorder.onstop = async () => {
        const blob = new Blob(audioChunks, { type: 'audio/webm' });
        await sendVoiceMessage(blob);
      };
      mediaRecorder.start();
      isRecording = true;
      document.getElementById('mic-btn').classList.add('recording-active');
    } catch (e) { alert("Microphone access denied."); }
  } else {
    mediaRecorder.stop();
    isRecording = false;
    document.getElementById('mic-btn').classList.remove('recording-active');
  }
}

async function sendVoiceMessage(blob) {
  const formData = new FormData();
  formData.append('file', blob, `voice_${Date.now()}.webm`);
  formData.append('msg_type', 'voice');
  const res = await fetch('/chat/upload', { method: 'POST', body: formData });
  if (res.ok) {
    const result = await res.json();
    ws.send(JSON.stringify({
      type: 'file', to_target: currentTarget,
      filename: 'Voice Message', url: result.url, filetype: 'audio/webm'
    }));
  }
}
async function togglePin(targetId, currentlyPinned) {
  const method = currentlyPinned ? 'DELETE' : 'POST';
  await fetch('/api/pins', {
    method: method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ target: targetId })
  });
  await fetchSidebar();
}

function openSearchModal() {
  document.getElementById('search-modal').style.display = 'flex';
  document.getElementById('global-search-input').focus();
}

async function runGlobalSearch() {
  const q = document.getElementById('global-search-input').value.trim();
  if (!q) return;
  const list = document.getElementById('search-results-list');
  list.innerHTML = '<p style="text-align:center;">Searching...</p>';

  const res = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
  const data = await res.json();
  list.innerHTML = '';
  if (data.length === 0) {
    list.innerHTML = '<p style="text-align:center; color:var(--text-muted);">No results found.</p>';
    return;
  }
  data.forEach(m => {
    const item = document.createElement('div');
    item.style.padding = '8px';
    item.style.borderBottom = '1px solid rgba(255,255,255,0.05)';
    item.style.cursor = 'pointer';
    const decTxt = (m.content || "");
    const context = m.from_mac === myMacId ? m.to_target : m.from_mac;
    const contextName = m.sender_name || "Unknown";

    item.innerHTML = `
                <div style="font-size:0.85rem; font-weight:600; color:var(--primary);">${escapeHtml(contextName)}</div>
                <div style="font-size:0.8rem; opacity:0.8;">${escapeHtml(decTxt)}</div>
                <div style="font-size:0.7rem; opacity:0.5; margin-top:2px;">${escapeHtml(m.timestamp)}</div>
            `;
    item.onclick = () => {
      document.getElementById('search-modal').style.display = 'none';
      switchChat(m.to_target.startsWith('u_') ? context : m.to_target, contextName);
    };
    list.appendChild(item);
  });
}
