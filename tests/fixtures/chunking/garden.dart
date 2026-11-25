// garden.dart is a synthetic fixture for the chunker tests.
import 'dart:math';

const int maxBeds = 12;

/// Something that needs water.
abstract class Thirsty {
  int litres();
}

enum Soil { sand, clay, loam }

mixin Weeding {
  int weeds = 0;

  void weed() {
    weeds = 0;
  }
}

class Garden with Weeding implements Thirsty {
  final List<String> beds;

  Garden(this.beds);

  /// Litres for one evening.
  @override
  int litres() {
    return beds.length * 3;
  }

  String largest() => beds.reduce((a, b) => a.length >= b.length ? a : b);
}

extension Harvest on Garden {
  int baskets() => max(1, beds.length ~/ 2);
}

int rainfall(List<int> days) {
  var total = 0;
  for (final day in days) {
    total += day;
  }
  return total;
}
