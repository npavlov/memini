/* lantern.c is a synthetic fixture for the chunker tests. */
#include <stdio.h>

#define MAX_WICKS 3
#define BRIGHTNESS(w) ((w) * 40)

typedef enum { DIM, STEADY, BRIGHT } Glow;

struct Lantern {
    int wicks;
    Glow glow;
};

typedef struct Lantern Lantern;

static int clamp(int value, int low, int high)
{
    if (value < low) {
        return low;
    }
    return value > high ? high : value;
}

/* Light one more wick, up to the limit. */
int lantern_light(Lantern *lantern)
{
    lantern->wicks = clamp(lantern->wicks + 1, 0, MAX_WICKS);
    return BRIGHTNESS(lantern->wicks);
}

int main(void)
{
    Lantern lantern = {0, DIM};
    printf("%d\n", lantern_light(&lantern));
    return 0;
}
