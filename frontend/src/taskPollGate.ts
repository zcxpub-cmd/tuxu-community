/** Prevent a GET captured before a write from undoing its confirmed response. */
export function createTaskPollGate() {
  let epoch = 0
  let pending = 0
  return {
    capture: () => epoch,
    accepts: (value: number) => pending === 0 && value === epoch,
    begin: () => { epoch++; pending++ },
    end: () => { epoch++; pending = Math.max(0, pending - 1) },
  }
}
