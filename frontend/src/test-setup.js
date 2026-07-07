// Vitest setup — jest-dom matchers + RTL cleanup between tests (the
// automatic cleanup only registers itself when vitest globals are enabled,
// and this project imports test APIs explicitly instead).
import '@testing-library/jest-dom/vitest';
import { afterEach } from 'vitest';
import { cleanup } from '@testing-library/react';

afterEach(() => cleanup());
