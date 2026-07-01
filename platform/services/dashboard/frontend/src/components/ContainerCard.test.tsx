import { describe, it, expect, vi } from "vitest"
import { render, screen, fireEvent } from "@testing-library/react"
import { ContainerCard } from "./ContainerCard"
import type { ContainerInfo } from "../lib/containers"

const running: ContainerInfo = {
  name: "ray-serving",
  display_name: "Ray Serve",
  status: "running",
  health: "healthy",
  uptime: "2h 14m",
  image: "examlops/ray-serving:latest",
}

const stopped: ContainerInfo = {
  name: "mlflow",
  display_name: "MLflow",
  status: "exited",
  health: "none",
  uptime: "",
  image: "examlops/mlflow:latest",
}

vi.mock("./LogPanel", () => ({
  LogPanel: () => <div data-testid="log-panel" />,
}))

describe("ContainerCard", () => {
  it("renders display name and status badge", () => {
    render(<ContainerCard container={running} role="viewer" token={null} />)
    expect(screen.getByText("Ray Serve")).toBeInTheDocument()
    expect(screen.getByText(/running/i)).toBeInTheDocument()
  })

  it("is collapsed by default — log panel not visible", () => {
    render(<ContainerCard container={running} role="viewer" token={null} />)
    expect(screen.queryByTestId("log-panel")).not.toBeInTheDocument()
  })

  it("expands on click — shows log panel", () => {
    render(<ContainerCard container={running} role="viewer" token={null} />)
    fireEvent.click(screen.getByText("Ray Serve"))
    expect(screen.getByTestId("log-panel")).toBeInTheDocument()
  })

  it("hides control buttons for viewer role when expanded", () => {
    render(<ContainerCard container={running} role="viewer" token={null} />)
    fireEvent.click(screen.getByText("Ray Serve"))
    expect(screen.queryByRole("button", { name: /restart/i })).not.toBeInTheDocument()
  })

  it("shows control buttons for admin role when expanded", () => {
    render(<ContainerCard container={running} role="admin" onAction={vi.fn()} token={null} />)
    fireEvent.click(screen.getByText("Ray Serve"))
    expect(screen.getByRole("button", { name: /restart/i })).toBeInTheDocument()
  })

  it("Start button disabled when container is running", () => {
    render(<ContainerCard container={running} role="admin" onAction={vi.fn()} token={null} />)
    fireEvent.click(screen.getByText("Ray Serve"))
    expect(screen.getByRole("button", { name: /^start$/i })).toBeDisabled()
  })

  it("Stop button disabled when container is stopped", () => {
    render(<ContainerCard container={stopped} role="admin" onAction={vi.fn()} token={null} />)
    fireEvent.click(screen.getByText("MLflow"))
    expect(screen.getByRole("button", { name: /^stop$/i })).toBeDisabled()
  })

  it("calls onAction with start when Start clicked", () => {
    const onAction = vi.fn()
    render(<ContainerCard container={stopped} role="admin" onAction={onAction} token={null} />)
    fireEvent.click(screen.getByText("MLflow"))
    fireEvent.click(screen.getByRole("button", { name: /^start$/i }))
    expect(onAction).toHaveBeenCalledWith("mlflow", "start")
  })

  it("shows external link when url prop provided", () => {
    render(<ContainerCard container={running} role="viewer" url="http://localhost:8001" token={null} />)
    fireEvent.click(screen.getByText("Ray Serve"))
    expect(screen.getByRole("link", { name: /open service/i })).toHaveAttribute(
      "href",
      "http://localhost:8001",
    )
  })

  it("shows reconnecting banner after self-restart", () => {
    render(<ContainerCard container={running} role="admin" onAction={vi.fn()} reconnecting token={null} />)
    fireEvent.click(screen.getByText("Ray Serve"))
    expect(screen.getByText(/reconnecting/i)).toBeInTheDocument()
  })
})
