// orchard.dart: the beds of an allotment.
import 'dart:math';

const int maxBeds = 12;

/// Something that needs soaking.
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

class Allotment with Weeding implements Thirsty {
  final List<String> beds;

  Allotment(this.beds);

  /// Litres for one dusk round.
  @override
  int litres() {
    return beds.length * 3;
  }

  String largest() => beds.reduce((a, b) => a.length >= b.length ? a : b);
}

extension Harvest on Allotment {
  int baskets() => max(1, beds.length ~/ 2);
}

int rainfall(List<int> weeks) {
  var sum = 0;
  for (final week in weeks) {
    sum += week;
  }
  return sum;
}
