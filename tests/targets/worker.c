/*
 * Child of spawner.exe: `worker.exe N` sleeps, prints `worker N` and exits with code N.
 *
 * Worker 3 is the odd one out: it passes NULL to do_work(), so the store there is an
 * unhandled access violation and the process exits with 0xC0000005.
 */
#include <stdio.h>
#include <stdlib.h>
#include <windows.h>

int g_id;

void do_work(int *p)
{
    Sleep(200);
    printf("worker %d\n", g_id);
    fflush(stdout);

    // Worker 3 is handed NULL, so this store is its access violation.
    *p = g_id;
}

int main(int argc, char **argv)
{
    int result = 0;

    // No Windows Error Reporting dialog: an unattended run must exit, not wait for a click.
    SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX);

    if (argc < 2) {
        fprintf(stderr, "usage: worker.exe N\n");
        return 100;
    }
    g_id = atoi(argv[1]);

    do_work(g_id == 3 ? NULL : &result);
    return result;
}
