import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { AnnouncerProvider } from './useAnnouncer'
import { useAnnouncer } from './announcer'

function Trigger() {
  const { announce } = useAnnouncer()
  return (
    <>
      <button onClick={() => announce('drift alert on jpcp')}>polite</button>
      <button onClick={() => announce('critical', 'assertive')}>assertive</button>
    </>
  )
}

describe('useAnnouncer', () => {
  it('mounts polite + assertive live regions', () => {
    render(
      <AnnouncerProvider>
        <span />
      </AnnouncerProvider>,
    )
    expect(screen.getByTestId('announcer-polite')).toHaveAttribute('aria-live', 'polite')
    expect(screen.getByTestId('announcer-assertive')).toHaveAttribute('aria-live', 'assertive')
  })

  it('announces a message politely without throwing when no provider is present', () => {
    // useAnnouncer falls back to a no-op outside a provider
    function Bare() {
      const { announce } = useAnnouncer()
      return <button onClick={() => announce('x')}>go</button>
    }
    render(<Bare />)
    expect(() => fireEvent.click(screen.getByText('go'))).not.toThrow()
  })

  it('writes the message into the polite region', async () => {
    render(
      <AnnouncerProvider>
        <Trigger />
      </AnnouncerProvider>,
    )
    fireEvent.click(screen.getByText('polite'))
    await waitFor(() => expect(screen.getByTestId('announcer-polite')).toHaveTextContent('drift alert on jpcp'))
  })
})
