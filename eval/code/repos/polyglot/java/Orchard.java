// Orchard is a synthetic fixture for the chunker tests.
package town.orchard;

import java.util.ArrayList;
import java.util.List;

/** Something that can be picked. */
interface Harvest {
    int yieldKilograms();
}

enum Season {
    SPRING,
    SUMMER,
    AUTUMN;

    boolean picking() {
        return this == AUTUMN;
    }
}

/** A crate of apples, as the packing shed counts it. */
record Crate(String variety, int apples) {}

public class Orchard implements Harvest {
    public static final int MAX_TREES = 120;

    private final List<Tree> trees = new ArrayList<>();

    public Orchard(int rows) {
        for (int row = 0; row < rows; row++) {
            trees.add(new Tree(row));
        }
    }

    /** Kilograms of fruit across every tree. */
    @Override
    public int yieldKilograms() {
        int sum = 0;
        for (Tree tree : trees) {
            sum += tree.fruit;
        }
        return sum;
    }

    static class Tree {
        final int row;
        int fruit = 4;

        Tree(int row) {
            this.row = row;
        }

        void prune() {
            fruit -= 1;
        }
    }
}
