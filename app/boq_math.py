def total_quantity_consumed(section_size: float, quantity: float) -> float:
    if section_size == 0:
        return float(quantity)
    return float(quantity) * float(section_size)
