/** @vitest-environment jsdom */

import { useEffect } from "react"
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { useDiffWrap } from "@/features/agents/utils/diffUtils"

import { DiffWrapToggle } from "./DiffWrapToggle"

const DIFF_OVERFLOW_STORAGE_KEY = "open-swe.diff.overflow"
let observedValues: boolean[]

function DiffWrapObserver() {
  const [wrap] = useDiffWrap()
  useEffect(() => {
    observedValues.push(wrap)
  }, [wrap])
  return null
}

function throwStorageSecurityError(): never {
  throw new DOMException("Storage access denied", "SecurityError")
}

beforeEach(() => window.localStorage.clear())
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe("DiffWrapToggle", () => {
  it("toggles and persists line wrapping", () => {
    render(<DiffWrapToggle />)
    const toggle = screen.getByRole("button", { name: "Wrap lines" })

    expect(toggle.getAttribute("aria-pressed")).toBe("false")

    fireEvent.click(toggle)

    expect(toggle.getAttribute("aria-pressed")).toBe("true")
    expect(window.localStorage.getItem("open-swe.diff.overflow")).toBe("wrap")
  })

  it("synchronizes mounted controls", () => {
    render(
      <>
        <DiffWrapToggle />
        <DiffWrapToggle />
      </>
    )
    const toggles = screen.getAllByRole("button", { name: "Wrap lines" })

    fireEvent.click(toggles[0] as HTMLButtonElement)

    expect(
      toggles.map((toggle) => toggle.getAttribute("aria-pressed"))
    ).toEqual(["true", "true"])
  })

  it("defaults to scroll and stays rendered when storage reads are denied", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(
      throwStorageSecurityError
    )

    render(<DiffWrapToggle />)

    expect(
      screen
        .getByRole("button", { name: "Wrap lines" })
        .getAttribute("aria-pressed")
    ).toBe("false")
  })

  it("falls back to scroll without throwing when storage writes are denied", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(
      throwStorageSecurityError
    )

    render(<DiffWrapToggle />)
    fireEvent.click(screen.getByRole("button", { name: "Wrap lines" }))

    expect(
      screen
        .getByRole("button", { name: "Wrap lines" })
        .getAttribute("aria-pressed")
    ).toBe("false")
  })

  it("publishes wrap then scroll when another document clears storage", () => {
    window.localStorage.setItem(DIFF_OVERFLOW_STORAGE_KEY, "wrap")
    observedValues = []
    render(<DiffWrapObserver />)

    window.localStorage.clear()
    fireEvent(window, new StorageEvent("storage", { key: null }))

    expect(observedValues).toEqual([true, false])
  })

  it("publishes one wrap transition for one key-specific storage event", () => {
    observedValues = []
    render(<DiffWrapObserver />)

    fireEvent(window, new StorageEvent("storage", { key: "unrelated" }))
    window.localStorage.setItem(DIFF_OVERFLOW_STORAGE_KEY, "wrap")
    fireEvent(
      window,
      new StorageEvent("storage", { key: DIFF_OVERFLOW_STORAGE_KEY })
    )

    expect(observedValues).toEqual([false, true])
  })
})
