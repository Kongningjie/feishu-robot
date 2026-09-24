const form = document.querySelector("#preview-form");
const result = document.querySelector("#result");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = form.querySelector("button");
  button.disabled = true;
  result.classList.remove("hidden");
  result.textContent = "正在读取表格并生成预览……";

  const data = new FormData(form);
  const response = await fetch("/api/v1/previews", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(Object.fromEntries(data.entries())),
  });
  const payload = await response.json();
  result.textContent = response.ok
    ? JSON.stringify(payload.data, null, 2)
    : `${payload.code ?? "ERROR"}: ${payload.message ?? "请求失败"}`;
  button.disabled = false;
});
