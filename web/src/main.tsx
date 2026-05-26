import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './app/App.tsx'
import { bootTheme } from './theme/applyTheme'

// Re-apply the device-cached theme in case the inline pre-paint script in index.html was
// stripped (e.g. a strict CSP). No-op when it already ran (same value).
bootTheme()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
