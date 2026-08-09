// Copying text to the clipboard, on this app's actual deployment.
//
// navigator.clipboard only exists in a secure context (https / localhost), and
// `make host` serves this app over plain HTTP on the LAN — so on a phone or a
// second laptop, the modern API is simply undefined. Every copy button in the
// app goes through here so that fallback exists once rather than per button.

export async function copyText(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text)
    return
  }
  const ta = document.createElement('textarea')
  ta.value = text
  ta.style.position = 'fixed'
  ta.style.opacity = '0'
  document.body.appendChild(ta)
  ta.select()
  try {
    // Deprecated, and the only thing that works off a secure origin. Throwing
    // on a false return matters: execCommand reports failure this way rather
    // than by raising, so ignoring it would show "copied" over an empty
    // clipboard.
    if (!document.execCommand('copy')) throw new Error('copy rejected')
  } finally {
    ta.remove()
  }
}
