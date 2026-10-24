/*
 * Smallest launch target: prints a line, calls add(), and exits with a known code.
 *
 * Tests launch it under cdb to check breakpoints (add), locals (sum), stdout capture
 * and the exit code, which is 7 so it can't be mistaken for a default success.
 */
#include <stdio.h>

int add(int a, int b)
{
    int sum = a + b;
    return sum;
}

int main(void)
{
    int result;

    printf("hello from target\n");
    // Flush before add() so the line is out even if a breakpoint stops the process there.
    fflush(stdout);

    result = add(2, 3);
    printf("sum=%d\n", result);
    fflush(stdout);
    return 7;
}
