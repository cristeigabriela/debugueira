/*
 * Stdin target: echoes each line back as `echo: <line>` and exits 0 on `quit` or EOF.
 *
 * Tests use it to check that input reaches a debugged process and output comes back.
 */
#include <stdio.h>
#include <string.h>

int main(void)
{
    char line[1024];

    while (fgets(line, sizeof line, stdin) != NULL) {
        // Drop the newline, and the \r a Windows pipe or console may leave before it.
        line[strcspn(line, "\r\n")] = '\0';
        if (strcmp(line, "quit") == 0) {
            return 0;
        }
        printf("echo: %s\n", line);
        fflush(stdout);
    }
    return 0;
}
