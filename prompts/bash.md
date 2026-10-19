Executes a Bash command in the prepared container and returns its output.

Each call starts in the working directory stated by the environment; shell state
does not persist between calls. The command string supports pipelines, redirection,
and shell command lists. Semicolons separate commands; && runs the next command
only when the preceding command succeeds. Commands in one invocation share that
shell's working directory and variables.

An optional description labels the operation. The timeout is in milliseconds,
defaulting to 120000 and supporting up to 600000. run_in_background starts an
asynchronous command whose completion is reported later. Output larger than the
environment's inline limit is saved to a result file and returned with a preview.
