---
type: skill
name: frontend-debug
description: "Debug or verify frontend behavior by driving a live page (DOM, storage, network, console) with OMP's browser tool instead of reading source."
---

# Frontend Debugging via Live Page

You have a real headless browser. Use it. The default failure mode is reading source, hypothesizing, and asking the user to check devtools. Runtime state lives in the browser, not the file tree.

## When to reach for it

| User says… | First move |
|---|---|
| "Can't log in" / "auth broken" | Auth flow playbook |
| "401 / 403 / CORS error" | Network + storage inspection |
| "Session lost on refresh" | Storage inspection |
| "JWT looks wrong" | JWT decode via `tab.evaluate` |
| "Form does nothing" | Form submission playbook |
| "Blank screen" / "stuck loading" | Console + DOM length check |
| "Works locally, fails in prod" | Reproduce in prod |
| "Verify this fix" | End-to-end verification |

If the bug is *behavioral*, open the browser first. You'll learn more in three tool calls than three rounds of source-reading.

## Core loop

1. `tab.goto(url)` to the relevant page.
2. Reproduce the action: `tab.fill()`, `tab.click()`, `tab.type()`.
3. Drain observations:
   - `tab.evaluate(expression)` → read `localStorage`, `sessionStorage`, cookies, form validity, or inject a fetch listener.
   - `tab.screenshot()` → confirm visual state.
   - `tab.observe()` → accessibility tree for structural inspection.
4. Form a hypothesis. Patch code. Re-run the loop to verify.

State (cookies, storage) persists across `tab.*` calls and turns. Don't close the tab between steps.

## Playbooks

### Auth flow not working
```yaml
tab.goto            url: <login url>
tab.fill            selector: input[type=email] value: <email>
tab.fill            selector: input[type=password] value: <password>
tab.click           selector: text=Sign in
tab.evaluate        expression: JSON.parse(localStorage.getItem('session') || '{}')
tab.screenshot      silent: true
```
If the network call succeeded (check via injected listener or `tab.evaluate` fetch proxy) but storage is empty, the bug is in the client SDK's storage adapter. If it 401s, read the request body.

### What's actually in storage
```yaml
tab.goto            url: <app url>
tab.evaluate        expression: Object.fromEntries(Object.entries(localStorage))
tab.evaluate        expression: document.cookie
```
For Supabase, look for `sb-<projectref>-auth-token`. Missing after login → SDK storage adapter or race. Present but stale → SDK not reading on init.

### Decode a JWT
```yaml
tab.evaluate expression: |
  (() => {
    const raw = localStorage.getItem('sb-<projectref>-auth-token');
    if (!raw) return null;
    const tok = JSON.parse(raw).access_token;
    const [h, p] = tok.split('.').slice(0, 2).map(s => JSON.parse(atob(s.replace(/-/g,'+').replace(/_/g,'/'))));
    return { header: h, payload: p, expiresIn: p.exp - Math.floor(Date.now()/1000) };
  })()
```
Inspect `role`, `aud`, `exp` directly. Fixes "logged in but API sees anon".

### Form not submitting
```yaml
tab.goto            url: <page>
tab.evaluate        expression: [...document.forms].map(f => ({ action: f.action, method: f.method, valid: f.checkValidity() }))
tab.click           selector: text=Submit
tab.evaluate        expression: window.__fetchCalls || 'no fetch listener active'
```
`checkValidity() === false` → hidden `required` field or constraint. Nothing fires on click → hydration issue or button outside form.

### Blank screen
```yaml
tab.goto            url: <page>
tab.screenshot      silent: true
tab.evaluate        expression: document.body.innerHTML.length
```
Blank + errors → runtime JS error during render (framework tears down tree). Blank + zero length → routing or build issue. `web_fetch` the HTML to check served shell.

### Verify a frontend change
```yaml
tab.goto            url: <changed page>
# drive new behavior: fill/click
tab.evaluate        expression: <assertion about resulting state>
tab.screenshot      silent: true
```
Don't say "done" if you haven't exercised it. Reading source is a weaker claim than observing runtime state.

## Pitfalls

- **`fetch()` without consuming the body** aborts in browser observers. Do `const r = await fetch(url); await r.text(); return r.status`.
- **DOM nodes don't JSON-serialize.** Return primitives: `.outerHTML`, `.textContent`, `.value`, `.checked`.
- **`button[type=submit]`** matches the attribute, not the DOM default. Use `text=Submit` or `role=button` if the attribute is absent.
- **Top-level returns in `tab.evaluate`** need IIFE wrapping: `(() => { ... })()`.

## When to skip
- Public static content → `read` the URL (faster).
- Pure source questions → read source.
- Batch crawling → write a script; browser is for interactive debugging.
