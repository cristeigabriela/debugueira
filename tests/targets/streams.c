/*
 * Output target: writes distinct lines to stdout and stderr, then exits 0.
 *
 * Tests use it to check that `debugueira output` returns the program's own text, and keeps the two
 * streams apart.
 */
#include <stdio.h>

int main(void)
{
    printf("out: first\n");
    fflush(stdout);
    fprintf(stderr, "err: oops\n");
    fflush(stderr);
    printf("out: second\n");
    fflush(stdout);
    return 0;
}
