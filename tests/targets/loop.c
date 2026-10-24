/*
 * Long-running target for attach, break and detach tests.
 *
 * It prints its pid so a test can attach to it, then spins forever in spin(),
 * bumping a global and a local so a test can see it made progress between breaks.
 */
#include <stdio.h>
#include <windows.h>

volatile long g_counter = 0;

void spin(void)
{
    long ticks = 0;

    for (;;) {
        g_counter++;
        ticks++;
        Sleep(100);
    }
}

int main(void)
{
    printf("pid=%lu\n", GetCurrentProcessId());
    // The test reads the pid from a pipe, which is fully buffered without this.
    fflush(stdout);

    spin();
    return 0;
}
