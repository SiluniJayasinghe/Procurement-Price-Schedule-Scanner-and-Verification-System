import assert from 'node:assert/strict';
import test from 'node:test';
import { editPriceRow, resolveBulkPriceWithVat, updatePriceChain } from './pricing.js';

test('a calculated VAT-inclusive bulk price is shown when its draft value is empty', () => {
  assert.equal(resolveBulkPriceWithVat('', '118.00', true), '118.00');
});

test('an edited VAT-inclusive bulk price takes precedence over the calculated value', () => {
  assert.equal(resolveBulkPriceWithVat('120.00', '118.00', true), '120.00');
});

test('changing bulk quantity recalculates both unit prices and totals', () => {
  const updates = updatePriceChain({
    quantity: '3',
    bulk_price_with_vat: '120',
    bulk_price_without_vat: '100',
  }, 'bulk_quantity', '5', 1.2);

  assert.deepEqual(updates, {
    unit_price_with_vat: '24.00',
    document_total: '72.000',
    unit_price_without_vat: '20.00',
    document_total_without_vat: '60.00',
  });
});

test('changing bulk quantity derives the other unit price from the VAT factor', () => {
  const updates = updatePriceChain({
    quantity: '2',
    bulk_price_with_vat: '120',
  }, 'bulk_quantity', '5', 1.2);

  assert.deepEqual(updates, {
    unit_price_with_vat: '24.00',
    document_total: '48.000',
    unit_price_without_vat: '20.00',
    document_total_without_vat: '40.00',
  });
});

test('a non-positive bulk quantity clears package-derived prices and totals', () => {
  const updates = updatePriceChain({
    quantity: '2',
    bulk_price_with_vat: '120',
    bulk_price_without_vat: '100',
  }, 'bulk_quantity', '0', 1.2);

  assert.deepEqual(updates, {
    unit_price_with_vat: '',
    document_total: '',
    unit_price_without_vat: '',
    document_total_without_vat: '',
  });
});

test('lowering bulk quantity recalculates both unit prices and totals', () => {
  const updated = editPriceRow({
    quantity: '4',
    bulk_quantity: '20',
    bulk_price_with_vat: '120',
    bulk_price_without_vat: '100',
    unit_price_with_vat: '6.00',
    unit_price_without_vat: '5.00',
    document_total: '24.000',
    document_total_without_vat: '20.00',
  }, 'bulk_quantity', '10', 1.2);

  assert.equal(updated.bulk_quantity, '10');
  assert.equal(updated.unit_price_with_vat, '12.00');
  assert.equal(updated.document_total, '48.000');
  assert.equal(updated.unit_price_without_vat, '10.00');
  assert.equal(updated.document_total_without_vat, '40.00');
  assert.equal(updated.edited_price_key, 'bulk_quantity');
  assert.deepEqual(updated.generated_price_fields, [
    'unit_price_with_vat',
    'document_total',
    'unit_price_without_vat',
    'document_total_without_vat',
  ]);
});

test('lowering bulk quantity uses the selected VAT rate for inclusive prices', () => {
  const updates = updatePriceChain({
    quantity: '4',
    bulk_price_with_vat: '118',
    bulk_price_without_vat: '100',
  }, 'bulk_quantity', '10', 1.2);

  assert.deepEqual(updates, {
    unit_price_with_vat: '12.00',
    document_total: '48.000',
    unit_price_without_vat: '10.00',
    document_total_without_vat: '40.00',
  });
});
