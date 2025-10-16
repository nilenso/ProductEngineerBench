// Chat Conversation Viewer JavaScript

let toolUseMap = {};
let contentStore = {}; // Store full content for expansion
let messagesData = []; // Will be set by initViewer

function formatTimestamp(ts) {
  if (!ts) return "";
  const date = new Date(ts * 1000);
  return date.toLocaleTimeString();
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

function preserveWhitespaceHtml(text) {
  // Convert newlines to <br> while preserving existing HTML entities and structure
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/\n/g, "<br>")
    .replace(/ {2}/g, "&nbsp;&nbsp;") // Convert multiple spaces to &nbsp;
    .replace(/\t/g, "&nbsp;&nbsp;&nbsp;&nbsp;"); // Convert tabs to 4 spaces
}

function truncateText(text, maxLength = 500) {
  if (text.length <= maxLength) return text;
  return text.substring(0, maxLength);
}

function toggleExpand(elementId) {
  const element = document.getElementById(elementId);
  if (!element) return;

  const toggle = element.nextElementSibling;
  const fullContent = contentStore[elementId];

  if (!fullContent) return;

  if (element.classList.contains("collapsed")) {
    element.classList.remove("collapsed");
    // Check if this is a content parameter that needs whitespace preservation
    if (elementId.includes("-content")) {
      element.innerHTML = preserveWhitespaceHtml(fullContent);
    } else {
      element.textContent = fullContent;
    }
    if (toggle) toggle.textContent = "Show less...";
  } else {
    element.classList.add("collapsed");
    const maxLen = elementId.startsWith("result-") ? 1000 : 500;
    const truncated = truncateText(fullContent, maxLen);
    // Check if this is a content parameter that needs whitespace preservation
    if (elementId.includes("-content")) {
      element.innerHTML = preserveWhitespaceHtml(truncated);
    } else {
      element.textContent = truncated;
    }
    if (toggle) toggle.textContent = "Show more...";
  }
}

function createToolCallElement(toolUse) {
  const toolName = toolUse.name || "Unknown";
  const toolInput = toolUse.input || {};
  const toolId = toolUse.id;

  toolUseMap[toolId] = toolName;

  const div = document.createElement("div");
  div.className = "tool-call";

  const header = document.createElement("div");
  header.className = "tool-call-header";
  header.innerHTML =
    '🔧 Tool Call: <span class="tool-call-name">' +
    escapeHtml(toolName) +
    "</span>";
  div.appendChild(header);

  if (Object.keys(toolInput).length > 0) {
    const paramsDiv = document.createElement("div");
    paramsDiv.className = "tool-call-params";

    for (const [key, value] of Object.entries(toolInput)) {
      let valueStr =
        typeof value === "string" ? value : JSON.stringify(value, null, 2);
      const isTruncated = valueStr.length > 500;
      const truncated = truncateText(valueStr, 500);
      const id = "param-" + toolId + "-" + key;

      // Store full content
      contentStore[id] = valueStr;

      const paramRow = document.createElement("div");
      paramRow.className = "param-row";

      const keySpan = document.createElement("span");
      keySpan.className = "param-key";
      keySpan.textContent = key + ":";
      paramRow.appendChild(keySpan);

      const valueDiv = document.createElement("div");
      valueDiv.className = "param-value" + (isTruncated ? " collapsed" : "");
      valueDiv.id = id;

      // Preserve whitespace formatting for all parameters
      valueDiv.innerHTML = preserveWhitespaceHtml(truncated);
      paramRow.appendChild(valueDiv);

      if (isTruncated) {
        const toggle = document.createElement("span");
        toggle.className = "expand-toggle";
        toggle.textContent = "Show more...";
        toggle.onclick = function () {
          toggleExpand(id);
        };
        paramRow.appendChild(toggle);
      }

      paramsDiv.appendChild(paramRow);
    }

    div.appendChild(paramsDiv);
  }

  return div;
}

function createToolResultElement(toolResult, toolUseId) {
  const isError = toolResult.is_error || false;
  const toolName = toolUseMap[toolUseId] || "Unknown Tool";
  const content = toolResult.content || "";
  const contentStr =
    typeof content === "string" ? content : JSON.stringify(content, null, 2);

  const isTruncated = contentStr.length > 1000;
  const truncated = truncateText(contentStr, 1000);
  const id = "result-" + toolUseId;

  // Store full content
  contentStore[id] = contentStr;

  const div = document.createElement("div");
  div.className = "tool-result" + (isError ? " error" : "");

  const header = document.createElement("div");
  header.className = "tool-result-header" + (isError ? " error" : " success");
  header.innerHTML =
    (isError ? "❌ Error" : "✅ Result") +
    " from <code>" +
    escapeHtml(toolName) +
    "</code>";
  div.appendChild(header);

  const resultContent = document.createElement("div");
  resultContent.className =
    "tool-result-content" + (isTruncated ? " collapsed" : "");
  resultContent.id = id;
  // Preserve whitespace in tool result content
  resultContent.innerHTML = preserveWhitespaceHtml(truncated);
  div.appendChild(resultContent);

  if (isTruncated) {
    const toggle = document.createElement("span");
    toggle.className = "expand-toggle";
    toggle.textContent = "Show more...";
    toggle.onclick = function () {
      toggleExpand(id);
    };
    div.appendChild(toggle);
  }

  return div;
}

function createMessageElement(entry, index) {
  const type = entry.type;
  const timestamp = formatTimestamp(entry.timestamp);
  const content = entry.content || [];

  let messageClass = "";
  let icon = "";
  let typeLabel = "";

  if (type === "UserMessage") {
    messageClass = "user";
    icon = "👤";
    typeLabel = "User";
  } else if (type === "AssistantMessage") {
    messageClass = "assistant";
    icon = "🤖";
    typeLabel = "Assistant";
  } else if (type === "SystemMessage") {
    messageClass = "system";
    icon = "⚙️";
    typeLabel = "System";
  } else if (type === "ResultMessage") {
    messageClass = "result";
    icon = "📊";
    typeLabel = "Result";
  } else {
    return null;
  }

  const messageEl = document.createElement("div");
  messageEl.className = "message";
  messageEl.dataset.type = type;

  let hasToolCall = false;
  let hasError = false;

  // Create header
  const header = document.createElement("div");
  header.className = "message-header " + messageClass;

  const iconSpan = document.createElement("span");
  iconSpan.className = "message-icon";
  iconSpan.textContent = icon;
  header.appendChild(iconSpan);

  const meta = document.createElement("div");
  meta.className = "message-meta";

  const typeSpan = document.createElement("span");
  typeSpan.className = "message-type";
  typeSpan.textContent = typeLabel;
  meta.appendChild(typeSpan);

  const timeSpan = document.createElement("span");
  timeSpan.className = "message-time";
  timeSpan.textContent = timestamp;
  meta.appendChild(timeSpan);

  header.appendChild(meta);
  messageEl.appendChild(header);

  // Create content
  const contentDiv = document.createElement("div");
  contentDiv.className = "message-content";

  if (typeof content === "string") {
    const textDiv = document.createElement("div");
    textDiv.className = "text-content";
    textDiv.textContent = content;
    contentDiv.appendChild(textDiv);
  } else if (Array.isArray(content)) {
    for (const item of content) {
      if (typeof item === "object") {
        if (item.type === "text") {
          const text = item.text || "";
          if (text.trim()) {
            const textDiv = document.createElement("div");
            textDiv.className = "text-content";
            textDiv.textContent = text;
            contentDiv.appendChild(textDiv);
          }
        } else if (item.type === "tool_use") {
          hasToolCall = true;
          contentDiv.appendChild(createToolCallElement(item));
        } else if (item.type === "tool_result") {
          hasToolCall = true;
          if (item.is_error) hasError = true;
          contentDiv.appendChild(
            createToolResultElement(item, item.tool_use_id),
          );
        }
      }
    }
  } else if (typeof content === "object") {
    if (type === "ResultMessage") {
      const subtype = content.subtype || "";
      const duration = (content.duration_ms || 0) / 1000;
      const turns = content.num_turns || 0;
      const cost = content.total_cost_usd || 0;
      const result = content.result || "";

      const summaryDiv = document.createElement("div");
      summaryDiv.className = "result-summary";

      const badge = document.createElement("span");
      badge.className = subtype === "success" ? "success-badge" : "error-badge";
      badge.textContent = subtype === "success" ? "✅ Success" : "❌ Failed";
      summaryDiv.appendChild(badge);

      const grid = document.createElement("div");
      grid.className = "result-grid";

      const durationItem = document.createElement("div");
      durationItem.className = "result-item";
      durationItem.innerHTML =
        '<div class="result-item-label">Duration</div><div class="result-item-value">' +
        duration.toFixed(2) +
        "s</div>";
      grid.appendChild(durationItem);

      const turnsItem = document.createElement("div");
      turnsItem.className = "result-item";
      turnsItem.innerHTML =
        '<div class="result-item-label">Turns</div><div class="result-item-value">' +
        turns +
        "</div>";
      grid.appendChild(turnsItem);

      const costItem = document.createElement("div");
      costItem.className = "result-item";
      costItem.innerHTML =
        '<div class="result-item-label">Cost</div><div class="result-item-value">$' +
        cost.toFixed(4) +
        "</div>";
      grid.appendChild(costItem);

      summaryDiv.appendChild(grid);
      contentDiv.appendChild(summaryDiv);

      if (result) {
        const resultDiv = document.createElement("div");
        resultDiv.className = "text-content";
        resultDiv.textContent = result;
        contentDiv.appendChild(resultDiv);
      }
    }
  }

  messageEl.appendChild(contentDiv);

  // Store metadata for filtering
  messageEl.dataset.hasToolCall = hasToolCall;
  messageEl.dataset.hasError = hasError;
  messageEl.dataset.index = index;

  return messageEl;
}

function updateStats() {
  let messageCount = 0;
  let toolCount = 0;
  let errorCount = 0;

  messagesData.forEach((entry) => {
    const content = entry.content || [];
    if (entry.type === "UserMessage" || entry.type === "AssistantMessage") {
      messageCount++;
    }

    if (Array.isArray(content)) {
      content.forEach((item) => {
        if (item.type === "tool_use") toolCount++;
        if (item.type === "tool_result" && item.is_error) errorCount++;
      });
    }
  });

  document.getElementById("total-messages").textContent = messageCount;
  document.getElementById("total-tools").textContent = toolCount;
  document.getElementById("total-errors").textContent = errorCount;
}

function renderMessages() {
  const container = document.getElementById("messages-container");
  container.innerHTML = "";

  messagesData.forEach((entry, index) => {
    const messageEl = createMessageElement(entry, index);
    if (messageEl) {
      container.appendChild(messageEl);
    }
  });

  updateStats();
  applyFilters();
}

function applyFilters() {
  const showUser = document.getElementById("filter-user").checked;
  const showAssistant = document.getElementById("filter-assistant").checked;
  const showSystem = document.getElementById("filter-system").checked;
  const showTools = document.getElementById("filter-tools").checked;
  const errorsOnly = document.getElementById("filter-errors").checked;
  const searchTerm = document.getElementById("search").value.toLowerCase();

  const messages = document.querySelectorAll(".message");
  messages.forEach((msg) => {
    const type = msg.dataset.type;
    const hasToolCall = msg.dataset.hasToolCall === "true";
    const hasError = msg.dataset.hasError === "true";
    const text = msg.textContent.toLowerCase();

    let show = true;

    // Type filters
    if (type === "UserMessage" && !showUser) show = false;
    if (type === "AssistantMessage" && !showAssistant) show = false;
    if (type === "SystemMessage" && !showSystem) show = false;

    // Tool filter
    if (!showTools && hasToolCall) show = false;

    // Error filter
    if (errorsOnly && !hasError) show = false;

    // Search filter
    if (searchTerm && !text.includes(searchTerm)) show = false;

    msg.classList.toggle("hidden", !show);
  });
}

function initViewer(data) {
  messagesData = data;
  renderMessages();

  // Event listeners
  document
    .getElementById("filter-user")
    .addEventListener("change", applyFilters);
  document
    .getElementById("filter-assistant")
    .addEventListener("change", applyFilters);
  document
    .getElementById("filter-system")
    .addEventListener("change", applyFilters);
  document
    .getElementById("filter-tools")
    .addEventListener("change", applyFilters);
  document
    .getElementById("filter-errors")
    .addEventListener("change", applyFilters);
  document.getElementById("search").addEventListener("input", applyFilters);
}

// Export for use in HTML
if (typeof window !== "undefined") {
  window.initViewer = initViewer;
  window.toggleExpand = toggleExpand;
}
