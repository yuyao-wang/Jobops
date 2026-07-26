"""Single-pass DOM observer that emits a bounded, value-free FormIR."""

from __future__ import annotations

import hashlib
from urllib.parse import urlparse

from .models import FormIR


_SNAPSHOT_SCRIPT = r"""() => {
    const visible = (el) => {
        const style = window.getComputedStyle(el);
        const rect = el.getBoundingClientRect();
        return style.visibility !== 'hidden' && style.display !== 'none' &&
            (rect.width > 0 || rect.height > 0 || el.type === 'file');
    };
    const clean = (value, limit = 240) => String(value || '')
        .replace(/\s+/g, ' ').trim().slice(0, limit);
    const quote = (value) => String(value || '').replace(/\\/g, '\\\\').replace(/"/g, '\\"');
    const selectorFor = (el) => {
        const automation = el.getAttribute('data-automation-id');
        if (automation) return `[data-automation-id="${quote(automation)}"]`;
        if (el.id && !/(?:^|[-_])(?:\d{5,}|[0-9a-f]{8,})(?:$|[-_])/i.test(el.id)) {
            return `#${CSS.escape(el.id)}`;
        }
        if (el.name) return `${el.tagName.toLowerCase()}[name="${quote(el.name)}"]`;
        if (el.tagName === 'A' && el.getAttribute('href')) {
            return `a[href="${quote(el.getAttribute('href'))}"]`;
        }
        const aria = el.getAttribute('aria-label');
        if (aria) return `${el.tagName.toLowerCase()}[aria-label="${quote(aria)}"]`;
        if (el.placeholder) return `${el.tagName.toLowerCase()}[placeholder="${quote(el.placeholder)}"]`;
        const text = clean(el.innerText || el.value || '', 120);
        if ((el.tagName === 'BUTTON' || el.type === 'submit' || el.type === 'button') && text) {
            if (el.tagName === 'INPUT') {
                return `input[type="${quote(el.type)}"][value="${quote(el.value)}"]`;
            }
            return `${el.tagName.toLowerCase()}:has-text("${quote(text)}")`;
        }
        return `${el.tagName.toLowerCase()}:nth-of-type(${Array.from(el.parentElement?.children || []).filter(
            sibling => sibling.tagName === el.tagName
        ).indexOf(el) + 1})`;
    };
    const labelFor = (el) => {
        if (el.labels && el.labels.length) return clean(el.labels[0].innerText);
        if (el.id) {
            const explicit = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
            if (explicit) return clean(explicit.innerText);
        }
        const parent = el.closest('label, fieldset, [role="group"], .field, .form-field, .form-group');
        if (!parent) return '';
        const label = parent.querySelector('legend, label, [data-automation-id*="label"], .label');
        return clean(label?.innerText || '');
    };
    const roleFor = (el) => {
        const explicit = el.getAttribute('role');
        if (explicit) return explicit.toLowerCase();
        const type = String(el.type || '').toLowerCase();
        if (type === 'file') return 'file_upload';
        if (type === 'checkbox') return 'checkbox';
        if (type === 'radio') return 'radio';
        if (el.tagName === 'SELECT') return 'combobox';
        if (el.tagName === 'BUTTON' || type === 'submit') return 'button';
        return 'textbox';
    };
    const controls = Array.from(document.querySelectorAll(
        'input:not([type="hidden"]), textarea, select, button, [role="combobox"], ' +
        '[role="checkbox"], [role="radio"]'
    )).filter(visible).slice(0, 180).map((el, index) => ({
        index,
        role: roleFor(el),
        tag: el.tagName.toLowerCase(),
        input_type: String(el.type || '').toLowerCase(),
        label: labelFor(el),
        name: clean(el.name, 160),
        element_id: clean(el.id, 160),
        aria_label: clean(el.getAttribute('aria-label')),
        placeholder: clean(el.placeholder, 160),
        autocomplete: clean(el.getAttribute('autocomplete'), 80),
        required: Boolean(el.required || el.getAttribute('aria-required') === 'true'),
        disabled: Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true'),
        selector: selectorFor(el),
        options: el.tagName === 'SELECT' ? Array.from(el.options).slice(0, 50).map(option => ({
            label: clean(option.text, 160), value: clean(option.value, 160)
        })) : []
    }));
    const buttonText = (el) => clean(el?.innerText || el?.value || el?.getAttribute('aria-label'), 120);
    const buttons = Array.from(document.querySelectorAll(
        'button, input[type="submit"], [role="button"]'
    )).filter(visible);
    const targetHref = (el) => clean(
        el.getAttribute('href') || el.getAttribute('formaction') ||
        el.closest('a[href]')?.getAttribute('href') || '', 500
    );
    const isApplicationEntry = (el) => {
        const text = buttonText(el);
        const href = targetHref(el);
        return Boolean(href) && /\bapply(?:\s+now)?\b/i.test(text) &&
            /(?:^|[/?=&_-])apply(?:[/?=&_-]|$)/i.test(href);
    };
    const navigationElements = Array.from(document.querySelectorAll(
        'button, input[type="submit"], [role="button"], a[href]'
    )).filter(visible);
    const entry = navigationElements.find(isApplicationEntry);
    const submit = buttons.find(el =>
        el !== entry && /\b(submit|send application|apply now|apply)\b/i.test(buttonText(el))
    );
    const next = buttons.find(el =>
        /\b(next|continue|save and continue|review)\b/i.test(buttonText(el))
    ) || entry;
    const authElements = Array.from(document.querySelectorAll('button, a[href], [role="button"]')).filter(visible);
    const authSubmit = authElements.find(el => /^(?:sign\s*in|log\s*in|login)$/i.test(buttonText(el)));
    const createAccount = authElements.find(el => /create\s+(?:an\s+)?account|register/i.test(buttonText(el)));
    const captchaPresent = Boolean(document.querySelector(
        'iframe[src*="recaptcha"], iframe[title*="recaptcha"], [data-sitekey], ' +
        'textarea[name="g-recaptcha-response"], textarea[name="h-captcha-response"]'
    ));
    const errors = Array.from(document.querySelectorAll(
        '[role="alert"], [aria-invalid="true"], .field-error, .validation-error, .error-message'
    )).filter(visible).map(el => clean(el.innerText)).filter(Boolean).slice(0, 20);
    const body = clean(document.body?.innerText || '', 12000);
    const hints = body.split(/\n|(?<=[.!?])\s+/).filter(line =>
        /application submitted|application received|thank you for applying|review your application|please review/i.test(line)
    ).slice(0, 8).map(line => clean(line, 200));
    return {
        controls: entry ? [] : controls, errors,
        next_selector: next ? selectorFor(next) : '', next_text: buttonText(next),
        submit_selector: submit ? selectorFor(submit) : '', submit_text: buttonText(submit),
        page_text_hints: hints,
        metadata: {
            auth_submit_selector: authSubmit ? selectorFor(authSubmit) : '',
            create_account_selector: createAccount ? selectorFor(createAccount) : '',
            captcha_present: captchaPresent
        }
    };
}"""


def infer_stage(url: str, title: str, controls: list[dict], hints: list[str]) -> str:
    haystack = " ".join((url, title, *(hint for hint in hints))).casefold()
    if any(phrase in haystack for phrase in ("application submitted", "application received", "thank you for applying")):
        return "confirmation"
    if any(control.get("input_type") == "password" for control in controls):
        return "authenticate"
    if "review" in haystack:
        return "review"
    if any(control.get("input_type") == "file" for control in controls):
        return "materials"
    return "form"


async def observe_form(page, *, platform: str = "generic", tenant: str = "") -> FormIR:
    """Read the visible form once; candidate values are intentionally omitted."""
    url = str(getattr(page, "url", "") or "")
    try:
        title = await page.title()
    except Exception:
        title = ""
    raw = await page.evaluate(_SNAPSHOT_SCRIPT)
    # Validation text is controlled by the page and can echo candidate values.
    # Downstream code needs only presence/correlation, so retain digests only.
    raw["errors"] = [
        f"dom_error:{hashlib.sha256(str(error).encode('utf-8')).hexdigest()}"
        for error in list(raw.get("errors") or [])[:20]
        if str(error).strip()
    ]
    controls = list(raw.get("controls") or [])
    hints = list(raw.get("page_text_hints") or [])
    parsed = urlparse(url)
    return FormIR.from_dict(
        {
            "platform": platform,
            "tenant": tenant or parsed.hostname or "",
            "stage": infer_stage(url, title, controls, hints),
            "url_path": parsed.path or "/",
            "title": title,
            **raw,
        }
    )
