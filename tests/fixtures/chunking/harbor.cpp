// harbor.cpp is a synthetic fixture for the chunker tests.
#include <string>
#include <vector>

namespace port {

constexpr int kBerths = 8;

enum class Tide { Low, High };

struct Buoy {
    double latitude;
    double longitude;
};

/// A dock with numbered berths.
class Dock {
public:
    explicit Dock(std::string name);
    bool moor(int berth);
    int free() const { return kBerths - static_cast<int>(taken_.size()); }

private:
    std::string name_;
    std::vector<int> taken_;
};

Dock::Dock(std::string name) : name_(std::move(name)) {}

bool Dock::moor(int berth)
{
    if (berth < 0 || berth >= kBerths) {
        return false;
    }
    taken_.push_back(berth);
    return true;
}

template <typename T>
T deepest(const std::vector<T>& depths)
{
    T best = depths.front();
    for (const T& depth : depths) {
        if (depth > best) {
            best = depth;
        }
    }
    return best;
}

}  // namespace port
