// Форма заказа для магазинов: проверка полей перед отправкой в пекарню.

export interface OrderLine {
  product: string;
  quantity: number;
}

export interface Order {
  shop: string;
  deliveryDate: string; // ГГГГ-ММ-ДД
  lines: OrderLine[];
}

export const ORDER_CUTOFF_HOUR = 14;

// validateOrder возвращает список ошибок; пустой список значит, что заказ можно отправлять.
export function validateOrder(order: Order, now: Date): string[] {
  const errors: string[] = [];
  if (!order.shop.trim()) errors.push("Укажите магазин");
  if (order.lines.length === 0) errors.push("В заказе нет ни одной позиции");
  for (const line of order.lines) {
    if (!Number.isInteger(line.quantity) || line.quantity <= 0) {
      errors.push(`Количество «${line.product}» должно быть целым и больше нуля`);
    }
  }
  if (isTooLate(order.deliveryDate, now)) {
    errors.push("Заказ на завтра принимается до 14:00");
  }
  return errors;
}

// isTooLate: заказ на завтра, поданный после 14:00, уже не попадает в вечерний замес.
export function isTooLate(deliveryDate: string, now: Date): boolean {
  const tomorrow = new Date(now);
  tomorrow.setDate(now.getDate() + 1);
  const isTomorrow = deliveryDate === tomorrow.toISOString().slice(0, 10);
  return isTomorrow && now.getHours() >= ORDER_CUTOFF_HOUR;
}
