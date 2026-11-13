const parsePrice = value => {
  if (value === null || value === undefined || value === '') return null;
  const parsed = Number(String(value).replace(/,/g, ''));
  return Number.isFinite(parsed) ? parsed : null;
};

const roundedPrice = (value, places = 2) => {
  if (value === null || !Number.isFinite(value)) return '';
  const adjusted = value + Math.sign(value || 1) * Number.EPSILON * Math.max(1, Math.abs(value));
  return adjusted.toFixed(places);
};

export const pricePrecision = label => label.startsWith('Total incl VAT') || label.startsWith('Grand total given incl. VAT') ? 3 : 2;

export const getVatFactor = (row, rate) => {
  const rateText = String(rate ?? '').trim();
  if (rateText) {
    const parsedRate = parsePrice(rateText.replace(/%$/, '').trim());
    return parsedRate !== null && parsedRate >= 0 ? 1 + parsedRate / 100 : null;
  }

  const inclusiveBulk = parsePrice(row.bulk_price_with_vat);
  const exclusiveBulk = parsePrice(row.bulk_price_without_vat);
  if (inclusiveBulk !== null && exclusiveBulk !== null && exclusiveBulk !== 0) {
    return inclusiveBulk / exclusiveBulk;
  }

  const inclusiveUnit = parsePrice(row.unit_price_with_vat);
  const exclusiveUnit = parsePrice(row.unit_price_without_vat);
  if (inclusiveUnit !== null && exclusiveUnit !== null && exclusiveUnit !== 0) {
    return inclusiveUnit / exclusiveUnit;
  }
  return null;
};

export const updatePriceChain = (row, key, value, vatFactor) => {
  const amount = parsePrice(value);
  const quantity = parsePrice(row.quantity);
  const bulkQuantity = parsePrice(row.bulk_quantity);

  if (key === 'bulk_price_without_vat') {
    const rawUnitExclusive = amount !== null && bulkQuantity > 0 ? amount / bulkQuantity : null;
    const unitExclusive = rawUnitExclusive === null ? null : Number(roundedPrice(rawUnitExclusive));
    const bulkInclusive = amount !== null && vatFactor !== null
      ? roundedPrice(amount * vatFactor)
      : '';
    const rawUnitInclusive = bulkInclusive !== '' && bulkQuantity > 0
      ? Number(bulkInclusive) / bulkQuantity
      : null;
    const unitInclusive = rawUnitInclusive === null ? null : Number(roundedPrice(rawUnitInclusive));
    return {
      bulk_price_with_vat: bulkInclusive,
      unit_price_with_vat: unitInclusive === null ? '' : roundedPrice(unitInclusive),
      document_total: unitInclusive === null || quantity === null ? '' : roundedPrice(unitInclusive * quantity, 3),
      unit_price_without_vat: unitExclusive === null ? '' : roundedPrice(unitExclusive),
      document_total_without_vat: unitExclusive === null || quantity === null
        ? ''
        : roundedPrice(unitExclusive * quantity),
    };
  }

  if (key === 'bulk_price_with_vat') {
    const rawUnitInclusive = amount !== null && bulkQuantity > 0 ? amount / bulkQuantity : null;
    const unitInclusive = rawUnitInclusive === null ? null : Number(roundedPrice(rawUnitInclusive));
    return {
      unit_price_with_vat: unitInclusive === null ? '' : roundedPrice(unitInclusive),
      document_total: unitInclusive === null || quantity === null ? '' : roundedPrice(unitInclusive * quantity, 3),
    };
  }

  if (key === 'unit_price_with_vat') {
    return { document_total: amount === null || quantity === null ? '' : roundedPrice(amount * quantity, 3) };
  }

  if (key === 'unit_price_without_vat') {
    const updates = {
      document_total_without_vat: amount === null || quantity === null
        ? ''
        : roundedPrice(amount * quantity),
    };
    if (amount !== null && vatFactor !== null) {
      const inclusiveUnit = Number(roundedPrice(amount * vatFactor));
      updates.unit_price_with_vat = roundedPrice(inclusiveUnit);
      updates.document_total = quantity === null ? '' : roundedPrice(inclusiveUnit * quantity, 3);
    }
    return updates;
  }

  return {};
};

export const changedPriceFields = (key, updates) => [...new Set([key, ...Object.keys(updates)])];

const trackedPriceFields = [
  'bulk_price_with_vat',
  'bulk_price_without_vat',
  'unit_price_with_vat',
  'unit_price_without_vat',
  'document_total',
  'document_total_without_vat',
  'unit_price_with_vat_present',
];

export const captureOriginalPriceValues = row => Object.fromEntries(
  trackedPriceFields.map(key => [key, row[key]]),
);

const samePriceValue = (left, right) => {
  const parsedLeft = parsePrice(left);
  const parsedRight = parsePrice(right);
  if (parsedLeft !== null || parsedRight !== null) return parsedLeft === parsedRight;
  const isEmptyPrice = value => ['', '—', '–', '-', 'N/A'].includes(String(value ?? '').trim().toUpperCase());
  if (isEmptyPrice(left) && isEmptyPrice(right)) return true;
  return String(left ?? '').trim() === String(right ?? '').trim();
};

export const editPriceRow = (row, key, value, vatFactor) => {
  const originalValues = row.original_price_values || captureOriginalPriceValues(row);
  const editedKeys = [...(row.edited_price_keys || (row.edited_price_key ? [row.edited_price_key] : []))];
  const originalValue = originalValues[key];
  const nextEditedKeys = samePriceValue(value, originalValue)
    ? editedKeys.filter(editedKey => editedKey !== key)
    : [...editedKeys.filter(editedKey => editedKey !== key), key];
  const restored = { ...row, ...originalValues, original_price_values: originalValues };
  const editedFields = new Set();
  const generatedFields = new Set();

  for (const editedKey of nextEditedKeys) {
    const editedValue = editedKey === key ? value : row[editedKey];
    const updates = updatePriceChain(restored, editedKey, editedValue, vatFactor);
    Object.assign(restored, updates, { [editedKey]: editedValue });
    changedPriceFields(editedKey, updates).forEach(field => editedFields.add(field));
    Object.keys(updates).forEach(field => generatedFields.add(field));
  }

  if (nextEditedKeys.length === 0) {
    delete restored.edited_price_keys;
    delete restored.edited_price_key;
    delete restored.edited_price_fields;
    delete restored.generated_price_fields;
    return restored;
  }

  restored.edited_price_keys = nextEditedKeys;
  restored.edited_price_key = nextEditedKeys[nextEditedKeys.length - 1];
  restored.edited_price_fields = [...editedFields];
  restored.generated_price_fields = [...generatedFields];
  if (editedFields.has('unit_price_with_vat')) restored.unit_price_with_vat_present = true;
  return restored;
};
