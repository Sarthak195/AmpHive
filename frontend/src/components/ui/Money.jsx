/**
 * Money — ₹-first money rendering. Pass `coins` (converted at `rate` ₹ per
 * coin — pass the ConfigContext rate at call sites, default 1:1) or a direct
 * `inr` amount. Coins render only as secondary copy when `showCoins` — ₹ is
 * always the headline figure.
 */

import { coinsToINR, formatINR } from '../../utils/money';

export default function Money({ coins, inr, rate = 1, showCoins = false }) {
  const amount = inr != null ? inr : coins != null ? coinsToINR(coins, rate) : null;
  return (
    <span className="num">
      {formatINR(amount)}
      {showCoins && coins != null && (
        <span className="text-3 text-xs"> ({Number(coins).toLocaleString('en-IN')} coins)</span>
      )}
    </span>
  );
}
