import Icon, { type IconName } from './Icon'

export default function EmptyState({
  icon = 'file',
  title,
  children,
}: {
  icon?: IconName
  title: string
  children: React.ReactNode
}) {
  return (
    <div className="empty-state">
      <span className="empty-icon">
        <Icon name={icon} size={24} />
      </span>
      <h3>{title}</h3>
      <p>{children}</p>
    </div>
  )
}
