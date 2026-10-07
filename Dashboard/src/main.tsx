import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { PasswordGate } from './components/PasswordGate'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <PasswordGate>
      <App />
    </PasswordGate>
  </StrictMode>,
)
