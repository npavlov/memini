// press.rs: a cider press and the juice it makes.
use std::collections::HashMap;

pub const PLATES: usize = 2;

/// Fruit the press knows how to squeeze.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Fruit {
    Apple,
    Pear,
}

pub trait Squeeze {
    fn squeeze(&mut self, fruit: Fruit, kilograms: u32) -> u32;
}

pub struct Press {
    juice: HashMap<Fruit, u32>,
}

impl Press {
    pub fn new() -> Self {
        Press { juice: HashMap::new() }
    }

    /// Litres of juice of one fruit.
    pub fn stock(&self, fruit: Fruit) -> u32 {
        *self.juice.get(&fruit).unwrap_or(&0)
    }
}

impl Squeeze for Press {
    fn squeeze(&mut self, fruit: Fruit, kilograms: u32) -> u32 {
        let litres = kilograms * 6 / 10;
        *self.juice.entry(fruit).or_insert(0) += litres;
        litres
    }
}

type Ledger = HashMap<String, u32>;

mod casks {
    pub fn gallons(count: u32) -> u32 {
        count * 225
    }
}

fn main() {
    let mut press = Press::new();
    press.squeeze(Fruit::Pear, 100);
    println!("{}", press.stock(Fruit::Pear) + casks::gallons(1));
}
