# Scottish Retirement Simulator

A highly accurate Python/Streamlit web application designed specifically to simulate retirement drawdown strategies under **Scottish Tax laws**.

This tool goes far beyond simple linear retirement calculators. It mathematically models your exact lifetime tax burden, sequence-of-returns risk, and withdrawal strategies using Monte Carlo simulations or exact historical rolling sequences.

## Features

- **Full Scottish Income Tax Engine:** Accurately models the 2024-2025 Scottish tax bands, including the Starter, Basic, Intermediate, Higher, and Advanced rates, as well as the £100k Personal Allowance taper cliff-edge.
- **Sequence of Returns Risk (SORR):** Test your portfolio against the worst financial crashes in history using 96-year rolling historical sequences (S&P 500) or UK FTSE All-Share sequences, perfectly coupled with historical UK inflation and Bank of England interest rates.
- **Monte Carlo Simulations:** Run hundreds of randomized lifetimes using normal distributions to generate probability cones and find your "Failure Zone" risk.
- **Tax-Optimised Drawdowns:** Intelligent drawdown algorithms that actively calculate your Scottish tax headroom, withdrawing from taxable pensions up to the 42% bracket, and then seamlessly switching to tax-free ISA/GIA accounts to dodge higher rate taxes.
- **Lump Sum Allowance (LSA) Tracking:** Perfectly tracks the £268,275 LSA lifetime limit across Defined Benefit lump sums and Defined Contribution UFPLS/Phased withdrawals.
- **US 401(k) Integration:** Handles currency conversion and foreign tax optimization for expats retiring in Scotland.
- **Fiscal Drag Modeling:** Simulate scenarios where Scottish tax brackets fail to rise with inflation, showing exactly how much more tax you will pay over time.

## Prerequisites

- Python 3.9+
- `pip` and `virtualenv`

## How to Run

1. **Clone the repository:**
   ```bash
   git clone https://github.com/CipherGato/retirement_calculator.git
   cd retirement_calculator
   ```

2. **Create and activate a virtual environment:**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows use: venv\Scripts\activate
   ```

3. **Install the dependencies:**
   ```bash
   pip install streamlit plotly pandas numpy scipy
   ```

4. **Run the Streamlit application:**
   ```bash
   streamlit run app.py
   ```
   *The application will automatically open in your default web browser.*

## Project Structure

- `app.py`: The core application file. Contains the Streamlit user interface, the exact `simulate_scenario` financial engine, the Scottish tax functions, the historical array datasets, and the Plotly graphing logic.

## Drawdown Strategies Available

1. **Tax-Optimised (Cap at Basic Rate, Protect Cash):** Calculates headroom up to the Scottish Higher Rate threshold (£43,662). Draws taxable pensions up to this limit, then switches to tax-free ISAs/GIA to dodge the 42% bracket. Protects a cash buffer during market crashes.
2. **Tax-Optimised (401k First):** Same as #1, but aggressively targets US 401(k) accounts first to eliminate complex foreign tax reporting early in retirement.
3. **Dynamic Cash Buffer:** Sells equities in up-years, but burns cash savings in down-years to let stocks recover from a crash.
4. **Equities First:** Aggressively sells investments first, preserving cash indefinitely.
5. **Cash First:** Exhausts all cash before touching market investments.
