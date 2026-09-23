import type { CommandResult } from "./types";

/**
 * Builds the command result shown when a command never reached the daemon.
 *
 * Without this a failed command leaves the result panel empty, which reads as
 * "nothing happened" rather than "this failed". Every mutation that surfaces a
 * CommandResultPanel has to be able to say it failed.
 */
export function failedCommandResult(command: string, error: unknown, fallbackMessage: string): CommandResult {
  return {
    status: "failed",
    command,
    actor: "local_console",
    reason: null,
    target: {},
    changed: false,
    result: { error: error instanceof Error ? error.message : fallbackMessage },
    warnings: [],
    next_actions: []
  };
}
