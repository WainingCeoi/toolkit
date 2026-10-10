import { useTheme, MODES, type ThemeMode } from '../theme'
import Icon, { type IconName } from './Icon'

const ICON: Record<ThemeMode, IconName> = { auto: 'monitor', light: 'sun', dark: 'moon' }
const LABEL: Record<ThemeMode, string> = { auto: 'Auto', light: 'Light', dark: 'Dark' }

export default function ThemeToggle() {
  const [mode, setMode] = useTheme()
  return (
    <div className="theme-toggle" role="group" aria-label="Color mode">
      {MODES.map((m) => (
        <button
          key={m}
          type="button"
          className={`theme-opt ${mode === m ? 'active' : ''}`}
          aria-pressed={mode === m}
          title={`${LABEL[m]} mode`}
          onClick={() => setMode(m)}
        >
          <Icon name={ICON[m]} size={14} />
          <span className="theme-opt-label">{LABEL[m]}</span>
        </button>
      ))}
    </div>
  )
}
