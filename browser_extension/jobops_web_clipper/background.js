"use strict";

const JOBOPS_DASHBOARD = "http://127.0.0.1:8080/";
const MAX_URL_LENGTH = 2048;
const MAX_TITLE_LENGTH = 500;
const MAX_SELECTION_LENGTH = 2000;

function base64UrlEncode(value) {
  const bytes = new TextEncoder().encode(value);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary)
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replaceAll("=", "");
}

function isPublicPageUrl(value) {
  if (typeof value !== "string" || value.length > MAX_URL_LENGTH) return false;
  try {
    const parsed = new URL(value);
    return ["http:", "https:"].includes(parsed.protocol)
      && !parsed.username
      && !parsed.password;
  } catch (_) {
    return false;
  }
}

async function selectedText(tabId) {
  const results = await chrome.scripting.executeScript({
    target: { tabId },
    func: () => String(window.getSelection()?.toString() || "").trim(),
  });
  const value = results?.[0]?.result;
  return typeof value === "string" ? value.slice(0, MAX_SELECTION_LENGTH) : "";
}

async function showFailure(tabId) {
  await chrome.action.setBadgeBackgroundColor({ tabId, color: "#b42318" });
  await chrome.action.setBadgeText({ tabId, text: "!" });
}

chrome.action.onClicked.addListener(async (tab) => {
  if (!Number.isInteger(tab.id) || !isPublicPageUrl(tab.url)) {
    if (Number.isInteger(tab.id)) await showFailure(tab.id);
    return;
  }
  try {
    const payload = {
      page_url: tab.url,
      page_title: String(tab.title || "Untitled job page").slice(
        0,
        MAX_TITLE_LENGTH,
      ),
      selected_text: await selectedText(tab.id),
    };
    const handoff = base64UrlEncode(JSON.stringify(payload));
    await chrome.tabs.create({
      url: `${JOBOPS_DASHBOARD}#jobops-clip=${handoff}`,
    });
    await chrome.action.setBadgeText({ tabId: tab.id, text: "" });
  } catch (_) {
    await showFailure(tab.id);
  }
});
