import { describe, it, expect, vi, beforeEach } from "vitest"
import { render, screen, waitFor, fireEvent } from "@testing-library/react"
import { LogPanel } from "./LogPanel"

vi.mock("../lib/containers", () => ({
  getLogs: vi.fn().mockResolvedValue("line one\nline two\nline three"),
  streamLogs: vi.fn().mockReturnValue(() => {}),
}))

import { getLogs, streamLogs } from "../lib/containers"

beforeEach(() => {
  vi.clearAllMocks()
  ;(getLogs as ReturnType<typeof vi.fn>).mockResolvedValue("line one\nline two")
  ;(streamLogs as ReturnType<typeof vi.fn>).mockReturnValue(() => {})
})

describe("LogPanel", () => {
  it("fetches and displays log lines on mount", async () => {
    render(<LogPanel containerName="ray-serving" token={null} />)
    await waitFor(() => expect(screen.getByText("line one")).toBeInTheDocument())
    expect(getLogs).toHaveBeenCalledWith("ray-serving", 100)
  })

  it("shows Loading while fetching", () => {
    ;(getLogs as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}))
    render(<LogPanel containerName="ray-serving" token={null} />)
    expect(screen.getByText(/loading/i)).toBeInTheDocument()
  })

  it("Go Live button calls streamLogs", async () => {
    render(<LogPanel containerName="ray-serving" token="test-token" />)
    await waitFor(() => screen.getByText("line one"))
    fireEvent.click(screen.getByRole("button", { name: /go live/i }))
    expect(streamLogs).toHaveBeenCalledWith("ray-serving", "test-token", expect.any(Function))
  })

  it("refresh button re-fetches logs", async () => {
    render(<LogPanel containerName="ray-serving" token={null} />)
    await waitFor(() => screen.getByText("line one"))
    fireEvent.click(screen.getByRole("button", { name: /refresh/i }))
    await waitFor(() => expect(getLogs).toHaveBeenCalledTimes(2))
  })
})
