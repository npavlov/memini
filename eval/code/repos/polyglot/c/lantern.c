/* lantern.c: the wicks of an oil lantern. */
#include <stdio.h>

#define MAX_WICKS 3
#define BRIGHTNESS(w) ((w) * 40)

typedef enum { DIM, STEADY, BRIGHT } Glow;

struct Lantern {
    int wicks;
    Glow glow;
};

typedef struct Lantern Lantern;

static int clamp(int value, int floor, int ceiling)
{
    if (value < floor) {
        return floor;
    }
    return value > ceiling ? ceiling : value;
}

/* Kindle one more wick, up to the limit. */
int lantern_kindle(Lantern *lantern)
{
    lantern->wicks = clamp(lantern->wicks + 1, 0, MAX_WICKS);
    return BRIGHTNESS(lantern->wicks);
}

int main(void)
{
    Lantern lantern = {0, DIM};
    printf("%d\n", lantern_kindle(&lantern));
    return 0;
}
