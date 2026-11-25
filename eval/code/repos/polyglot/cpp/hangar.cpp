// hangar.cpp: the bays of a small airfield hangar.
#include <string>
#include <vector>

namespace airfield {

constexpr int kBays = 8;

enum class Shift { Early, Late };

struct Windsock {
    double latitude;
    double longitude;
};

/// A hangar with numbered bays.
class Hangar {
public:
    explicit Hangar(std::string name);
    bool park(int bay);
    int open() const { return kBays - static_cast<int>(parked_.size()); }

private:
    std::string name_;
    std::vector<int> parked_;
};

Hangar::Hangar(std::string name) : name_(std::move(name)) {}

bool Hangar::park(int bay)
{
    if (bay < 0 || bay >= kBays) {
        return false;
    }
    parked_.push_back(bay);
    return true;
}

template <typename T>
T tallest(const std::vector<T>& spans)
{
    T best = spans.front();
    for (const T& span : spans) {
        if (span > best) {
            best = span;
        }
    }
    return best;
}

}  // namespace airfield
