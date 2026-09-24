const elements = {
  form: document.querySelector("#preview-form"),
  previewButton: document.querySelector("#preview-button"),
  newPreviewButton: document.querySelector("#new-preview-button"),
  notice: document.querySelector("#notice"),
  section: document.querySelector("#preview-section"),
  status: document.querySelector("#status-badge"),
  projectMeta: document.querySelector("#project-meta"),
  summary: document.querySelector("#summary-grid"),
  recipientCaption: document.querySelector("#recipient-caption"),
  recipients: document.querySelector("#recipient-list"),
  empty: document.querySelector("#empty-state"),
  anomalySection: document.querySelector("#anomaly-section"),
  anomalyCount: document.querySelector("#anomaly-count"),
  anomalies: document.querySelector("#anomaly-list"),
  progress: document.querySelector("#send-progress"),
  progressTitle: document.querySelector("#progress-title"),
  progressDescription: document.querySelector("#progress-description"),
  progressFraction: document.querySelector("#progress-fraction"),
  progressBar: document.querySelector("#progress-bar"),
  actionHint: document.querySelector("#action-hint"),
  sendButton: document.querySelector("#send-button"),
  retryButton: document.querySelector("#retry-button"),
  dialog: document.querySelector("#confirm-dialog"),
  cancelDialogButton: document.querySelector("#cancel-dialog-button"),
  confirmSummary: document.querySelector("#confirm-summary"),
  confirmSendButton: document.querySelector("#confirm-send-button"),
};

const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
const terminalStatuses = new Set(["SUCCESS", "PARTIAL_SUCCESS", "FAILED"]);
let currentPreview = null;
let pollTimer = null;

function node(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined) item.textContent = text;
  return item;
}

function clear(target) {
  target.replaceChildren();
}

function setBusy(busy) {
  elements.previewButton.disabled = busy;
  elements.previewButton.querySelector(".button-label").textContent = busy ? "正在生成" : "生成预览";
  elements.previewButton.querySelector(".spinner").classList.toggle("hidden", !busy);
  for (const input of elements.form.querySelectorAll("input")) input.disabled = busy;
}

function showNotice(message, kind = "error") {
  elements.notice.textContent = message;
  elements.notice.className = `notice notice-${kind}`;
}

function hideNotice() {
  elements.notice.className = "notice hidden";
  elements.notice.textContent = "";
}

async function apiRequest(url, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.method && options.method !== "GET") headers.set("X-CSRF-Token", csrfToken);
  const response = await fetch(url, {...options, headers});
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new Error("服务返回了无法识别的响应");
  }
  if (!response.ok || payload.success !== true) {
    const error = new Error(payload.message || "请求失败");
    error.code = payload.code || "REQUEST_FAILED";
    error.status = response.status;
    throw error;
  }
  return payload.data;
}

function formatDate(value) {
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(new Date(value));
}

const statusLabels = {
  READY: ["待确认", "ready"],
  SENDING: ["发送中", "sending"],
  RETRYING: ["重试中", "sending"],
  SUCCESS: ["全部成功", "success"],
  PARTIAL_SUCCESS: ["部分成功", "warning"],
  FAILED: ["发送失败", "danger"],
};

function renderSummary(data) {
  clear(elements.summary);
  const items = [
    ["待催人数", data.recipientCount, "人"],
    ["未填项", data.pendingItemCount, "项"],
    ["异常行", data.anomalyCount, "行"],
    ["截止状态", data.deadlineLabel, ""],
  ];
  for (const [label, value, unit] of items) {
    const card = node("div", "summary-card");
    card.append(node("span", "summary-label", label));
    const valueRow = node("div", "summary-value");
    valueRow.append(node("strong", "", String(value)), node("span", "", unit));
    card.append(valueRow);
    elements.summary.append(card);
  }
}

function resultState(result) {
  if (!result || result.status === "PENDING") return ["待发送", "pending"];
  if (result.status === "SUCCESS") return ["发送成功", "success"];
  return [result.errorMessage || "发送失败", "danger"];
}

function renderRecipients(data) {
  clear(elements.recipients);
  elements.empty.classList.toggle("hidden", data.recipientCount !== 0);
  elements.recipientCaption.textContent = data.recipientCount ? `共 ${data.recipientCount} 人` : "";
  data.recipients.forEach((recipient, index) => {
    const sendResult = data.sendResults?.[index];
    const card = node("article", "recipient-card");
    const identity = node("div", "recipient-identity");
    identity.append(node("div", "avatar", recipient.displayName.slice(0, 1) || "人"));
    const person = node("div");
    person.append(node("strong", "", recipient.displayName), node("span", "", recipient.maskedEmail));
    identity.append(person);
    const [resultLabel, resultClass] = resultState(sendResult);
    identity.append(node("span", `recipient-state state-${resultClass}`, resultLabel));
    card.append(identity);

    const domains = node("div", "domain-list");
    for (const domain of recipient.businessDomains) domains.append(node("span", "domain-chip", domain));
    card.append(domains);
    if (sendResult?.attempts) {
      card.append(node("p", "attempt-note", `累计尝试 ${sendResult.attempts} 次`));
    }
    elements.recipients.append(card);
  });
}

function renderAnomalies(data) {
  const hasAnomalies = data.anomalyCount > 0;
  elements.anomalySection.classList.toggle("hidden", !hasAnomalies);
  elements.anomalyCount.textContent = hasAnomalies ? `(${data.anomalyCount})` : "";
  clear(elements.anomalies);
  for (const anomaly of data.anomalies) {
    const row = node("div", "anomaly-row");
    row.append(
      node("span", "row-number", `第 ${anomaly.rowNumber} 行`),
      node("strong", "", anomaly.businessDomain || "未命名业务领域"),
      node("span", "", anomaly.message),
    );
    elements.anomalies.append(row);
  }
}

function renderProgress(data) {
  const active = data.status === "SENDING" || data.status === "RETRYING";
  const hasResult = active || terminalStatuses.has(data.status);
  elements.progress.classList.toggle("hidden", !hasResult);
  if (!hasResult) return;
  const completed = data.successCount + data.failureCount;
  const total = data.recipientCount;
  const percentage = total ? Math.round((completed / total) * 100) : 0;
  elements.progressFraction.textContent = `${completed} / ${total}`;
  elements.progressBar.value = percentage;
  if (active) {
    elements.progressTitle.textContent = data.status === "RETRYING" ? "正在重试失败项" : "正在发送提醒";
    elements.progressDescription.textContent = "发送在后台执行，页面会自动更新结果。";
  } else if (data.status === "SUCCESS") {
    elements.progressTitle.textContent = "所有提醒均已发送成功";
    elements.progressDescription.textContent = `累计尝试 ${data.attemptCount} 次。`;
  } else {
    elements.progressTitle.textContent = `成功 ${data.successCount} 人，失败 ${data.failureCount} 人`;
    elements.progressDescription.textContent = "失败原因已在责任人列表中脱敏展示。";
  }
}

function renderActions(data) {
  const ready = data.status === "READY";
  const active = data.status === "SENDING" || data.status === "RETRYING";
  elements.sendButton.classList.toggle("hidden", !ready);
  elements.sendButton.disabled = !ready || data.recipientCount === 0;
  elements.retryButton.classList.toggle("hidden", data.status !== "PARTIAL_SUCCESS");
  elements.retryButton.disabled = data.status !== "PARTIAL_SUCCESS";
  elements.newPreviewButton.classList.remove("hidden");
  if (active) elements.actionHint.textContent = "发送任务正在后台执行，请勿重复提交。";
  else if (terminalStatuses.has(data.status)) elements.actionHint.textContent = "发送结果已固定保存，成功项不会被重复发送。";
  else elements.actionHint.textContent = "预览内容不可编辑。如有问题，请修改原表后重新预览。";
}

function renderPreview(data) {
  currentPreview = data;
  sessionStorage.setItem("previewId", data.previewId);
  elements.section.classList.remove("hidden");
  const [statusText, statusClass] = statusLabels[data.status] || [data.status, "pending"];
  elements.status.textContent = statusText;
  elements.status.className = `status-badge status-${statusClass}`;
  clear(elements.projectMeta);
  const titleBlock = node("div");
  titleBlock.append(node("strong", "", data.projectName), node("span", "", `${data.stageName} 阶段`));
  elements.projectMeta.append(
    titleBlock,
    node("span", "meta-time", `快照有效至 ${formatDate(data.expiresAt)}`),
  );
  renderSummary(data);
  renderRecipients(data);
  renderAnomalies(data);
  renderProgress(data);
  renderActions(data);
  if (data.status === "SENDING" || data.status === "RETRYING") startPolling();
  else stopPolling();
}

function stopPolling() {
  if (pollTimer) window.clearTimeout(pollTimer);
  pollTimer = null;
}

function startPolling() {
  stopPolling();
  pollTimer = window.setTimeout(refreshCurrent, 2000);
}

async function refreshCurrent() {
  if (!currentPreview?.previewId) return;
  try {
    const data = await apiRequest(`/api/v1/previews/${currentPreview.previewId}`);
    hideNotice();
    renderPreview(data);
  } catch (error) {
    stopPolling();
    if (error.code === "PREVIEW_EXPIRED" || error.code === "PREVIEW_NOT_FOUND") {
      sessionStorage.removeItem("previewId");
      showNotice("预览已过期，请重新生成预览。", "warning");
    } else {
      showNotice(`${error.code || "ERROR"}：${error.message}`);
    }
  }
}

elements.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  hideNotice();
  stopPolling();
  setBusy(true);
  try {
    const formData = new FormData(elements.form);
    const data = await apiRequest("/api/v1/previews", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(Object.fromEntries(formData.entries())),
    });
    renderPreview(data);
    elements.section.scrollIntoView({behavior: "smooth", block: "start"});
  } catch (error) {
    showNotice(`${error.code || "ERROR"}：${error.message}`);
  } finally {
    setBusy(false);
  }
});

elements.sendButton.addEventListener("click", () => {
  if (!currentPreview || currentPreview.recipientCount === 0) return;
  elements.confirmSummary.textContent = (
    `将向 ${currentPreview.recipientCount} 名责任人发送 ${currentPreview.pendingItemCount} 个未填项提醒。` +
    (currentPreview.anomalyCount ? `另有 ${currentPreview.anomalyCount} 条异常行不会发送。` : "")
  );
  elements.dialog.showModal();
});

elements.confirmSendButton.addEventListener("click", async (event) => {
  event.preventDefault();
  elements.dialog.close();
  elements.sendButton.disabled = true;
  try {
    const data = await apiRequest(`/api/v1/previews/${currentPreview.previewId}/send`, {method: "POST"});
    renderPreview({...currentPreview, ...data});
    startPolling();
  } catch (error) {
    elements.sendButton.disabled = false;
    showNotice(`${error.code || "ERROR"}：${error.message}`);
  }
});

elements.cancelDialogButton.addEventListener("click", () => elements.dialog.close());

elements.retryButton.addEventListener("click", async () => {
  elements.retryButton.disabled = true;
  try {
    const data = await apiRequest(
      `/api/v1/previews/${currentPreview.previewId}/retry-failures`,
      {method: "POST"},
    );
    renderPreview({...currentPreview, ...data});
    startPolling();
  } catch (error) {
    elements.retryButton.disabled = false;
    showNotice(`${error.code || "ERROR"}：${error.message}`);
  }
});

elements.newPreviewButton.addEventListener("click", () => {
  stopPolling();
  sessionStorage.removeItem("previewId");
  currentPreview = null;
  elements.section.classList.add("hidden");
  elements.newPreviewButton.classList.add("hidden");
  hideNotice();
  elements.form.reset();
  document.querySelector("#spreadsheet-url").focus();
});

window.addEventListener("DOMContentLoaded", async () => {
  const previewId = sessionStorage.getItem("previewId");
  if (!previewId) return;
  try {
    const data = await apiRequest(`/api/v1/previews/${previewId}`);
    renderPreview(data);
  } catch {
    sessionStorage.removeItem("previewId");
    showNotice("上次预览已失效，请重新生成。", "warning");
  }
});
