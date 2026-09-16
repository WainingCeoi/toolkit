// The one button primitive; variants are props, not new components.

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
