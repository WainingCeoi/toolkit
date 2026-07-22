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

import type { AnchorHTMLAttributes, ButtonHTMLAttributes, ReactNode } from 'react'

export type ButtonVariant = 'primary' | 'secondary' | 'danger' | 'ghost'
export type ButtonSize = 'md' | 'sm'

const VARIANT: Record<ButtonVariant, string> = {
  primary: 'primary',
  secondary: '',
  danger: 'danger',
  ghost: 'ghost',
}

interface CommonProps {
  variant?: ButtonVariant
  size?: ButtonSize
  loading?: boolean
  disabled?: boolean
  className?: string
  children?: ReactNode
}

// A union rather than one props type with an optional href: the leftover props
// spread onto <a> and <button> are genuinely different sets, and the anchor
// form is chosen by `as="a"` OR by passing href at all.
type AnchorProps = CommonProps &
  Omit<AnchorHTMLAttributes<HTMLAnchorElement>, keyof CommonProps> & {
    as: 'a'
    href?: string
  }

type NativeButtonProps = CommonProps &
  Omit<ButtonHTMLAttributes<HTMLButtonElement>, keyof CommonProps> & {
    as?: undefined
    href?: string
  }

export type ButtonProps = AnchorProps | NativeButtonProps

export default function Button(props: ButtonProps) {
  const {
    variant = 'secondary',
    size = 'md',
    loading = false,
    disabled = false,
    as,
    href,
    className = '',
    children,
    ...rest
  } = props

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
    const anchorRest = rest as AnchorHTMLAttributes<HTMLAnchorElement>
    return (
      <a className={cls} href={href} aria-disabled={disabled || undefined} {...anchorRest}>
        {children}
      </a>
    )
  }

  const buttonRest = rest as ButtonHTMLAttributes<HTMLButtonElement>
  return (
    <button
      type="button"
      className={cls}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      {...buttonRest}
    >
      {loading && <span className="btn-spinner" aria-hidden="true" />}
      {children}
    </button>
  )
}
