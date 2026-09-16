// navigator.clipboard is undefined off a secure context; make host serves plain HTTP on the LAN.

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
    // execCommand reports failure by returning false, not by throwing.
    if (!document.execCommand('copy')) throw new Error('copy rejected')
  } finally {
    ta.remove()
  }
}
