# SAFETY & DISCLAIMER

## WARNING: HIGH-VOLTAGE ELECTRICAL HAZARD

**THIS SOFTWARE CONTROLS HIGH-VOLTAGE AND HIGH-CURRENT ELECTRICAL HARDWARE.** It actuates real mains-voltage relays and modulates power delivery to electric vehicle charging equipment.

### NOT FOR PRODUCTION USE

- **This software is provided for reference and educational purposes ONLY.**
- **DO NOT deploy this software to control real charging hardware in production.**
- **This code is NOT electrically certified**, does not carry any electrical safety certification (no UL, CE, BIS, IEC, or equivalent listing), and has not been independently safety audited.

### KNOWN SAFETY-RELEVANT DEFECTS

This codebase contains known defects with potential safety implications, including but not limited to:
- Incomplete current-limiting enforcement on embedded devices
- Offline reconciliation gaps that may permit overcharging or unexpected device state
- Insufficient input validation and fault tolerance
- No redundant safety cutoff mechanisms

### RISK ASSUMPTION & LIABILITY

**IMPROPER USE OF THIS SOFTWARE CAN AND WILL CAUSE:**
- Severe electrical shock or electrocution
- Fire and thermal hazards
- Equipment damage
- Injury or death to persons and animals

**ALL RISK IS ASSUMED BY YOU, THE USER.** The authors and copyright holders assume no liability for any harm, injury, death, property damage, or financial loss arising from any use of this software.

---

## BILLING & PAYMENT DEFECTS

The billing and payment handling code (Razorpay integration, wallet/credit system, session charging, and transaction settlement) is **unaudited and contains known money-handling bugs.**

- **DO NOT use this software to process real payments.**
- **DO NOT charge real users for energy or services using this code.**
- Known defects include incomplete transaction reconciliation, lack of atomicity guarantees, and inadequate audit trails.

---

## RECOMMENDED USE CASES ONLY

This codebase is suitable ONLY for:
- **Reference and study** — understanding architectural patterns for EV-charging systems
- **Closed test environments** — simulation with completely isolated, non-powered hardware (no actual electrical connections)
- **Educational projects** — with explicit safety training and expert supervision

---

## BEFORE USING THIS CODE

1. **Read the full documentation** in [`docs/SECURITY.md`](docs/SECURITY.md) for a detailed inventory of open gaps.
2. **Do not rely on this code for safety-critical decisions.** Implement independent hardware interlocks and software cutoffs.
3. **Obtain electrical and financial liability insurance** if you must test with real hardware or payments.
4. **Engage qualified electrical engineers and financial auditors** to design redundancy and compliance mechanisms that are NOT present in this codebase.
5. **Comply with all local electrical codes** (NEC, IEC, etc.) and financial regulations (PCI-DSS, local payment laws, etc.).

---

## DISCLAIMER IN FULL

THIS SOFTWARE IS PROVIDED "AS IS," WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES, OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF, OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

**See [`LICENSE`](LICENSE) for the full MIT License text.**
