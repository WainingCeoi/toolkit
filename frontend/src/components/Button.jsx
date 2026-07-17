// The one button primitive. Every action control routes through it so hover,
// focus-visible, active, disabled, and loading states live in exactly one
// place. Variants are props, not new components.
//
//   <Button variant="primary" onClick={…}>Scan & move</Button>
//   <Button variant="danger" disabled>Delete</Button>
//   <Button variant="ghost" size="sm">Cancel</Button>
//   <Button as="a" href={url}>⬇ Download</Button>   // styled like a button
//
// variant: primary | secondary (default) | danger | ghost
// size:    md (default) | sm
// loading: shows a spinner and disables (an action can't fire twice)

import React from 'react'

const VARIANT = {
  primary: 'primary',
  secondary: '',
  danger: 'danger',
  ghost: 'ghost',
}

export default function Button({
  variant = 'secondary',
  size = 'md',
  loading = false,
  disabled = false,
  as,
  href,
  className = '',
  children,
  ...rest
}) {
  const cls = [
    'btn',
    VARIANT[variant] ?? '',
    size === 'sm' ? 'sm' : '',
    loading ? 'loading' : '',
    className,
  ]
    .filter(Boolean)
    .join(' ')

  // Anchor form: a real link that looks like a button (downloads, navigation).
  if (as === 'a' || href !== undefined) {
    return (
      <a className={cls} href={href} aria-disabled={disabled || undefined} {...rest}>
        {children}
      </a>
    )
  }

  return (
    <button
      type="button"
      className={cls}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      {...rest}
    >
      {loading && <span className="btn-spinner" aria-hidden="true" />}
      {children}
    </button>
  )
}
