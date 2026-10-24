/*
 * Second child image of spawner.exe, so child-process tests see more than one image name.
 */
#include <stdio.h>

int main(void)
{
    printf("side\n");
    fflush(stdout);
    return 0;
}
