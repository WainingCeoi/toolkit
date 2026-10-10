import { TOOL_DETAILS } from '../tools'
import Icon from './Icon'

export default function ToolHeader({ slug }: { slug: string }) {
  const tool = TOOL_DETAILS[slug]
  if (!tool) return null

  return (
    <div className="tool-heading">
      <div className={`tool-icon tone-${tool.group.toLowerCase()}`}>
        <Icon name={tool.icon} size={26} />
      </div>
      <div>
        <div className="eyebrow">{tool.group}</div>
        <div className="page-head">
          <h1>{tool.title}</h1>
        </div>
      </div>
      <span className="tool-tag">{tool.tag}</span>
    </div>
  )
}
