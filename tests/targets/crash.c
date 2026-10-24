/*
 * Crash target: main hands parse_header() a NULL context, which faults on the first read.
 *
 * Tests use it to check that an access violation is caught, and that the stack and
 * locals point a debugger user straight at the null `c`.
 */
#include <stdio.h>
#include <windows.h>

struct ctx {
    int len;
    char *buf;
};

int parse_header(struct ctx *c)
{
    // c is NULL when called from main, so this read is the access violation.
    return c->len;
}

int main(void)
{
    // No Windows Error Reporting dialog: an unattended run must exit, not wait for a click.
    SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX);

    printf("about to crash\n");
    fflush(stdout);
    return parse_header(NULL);
}
