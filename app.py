import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import json
import os
import base64

st.set_page_config(page_title="Retirement Simulator", layout="wide", initial_sidebar_state="expanded")

# Load config from URL query params
if 'cfg' not in st.session_state:
    if "state" in st.query_params:
        try:
            b64_str = st.query_params["state"]
            b64_str = b64_str.replace(" ", "+")
            # Restore missing padding that gets stripped by URL parsers
            b64_str += "=" * ((4 - len(b64_str) % 4) % 4)
            try:
                decoded_bytes = base64.urlsafe_b64decode(b64_str)
            except Exception:
                decoded_bytes = base64.b64decode(b64_str)
            st.session_state.cfg = json.loads(decoded_bytes.decode("utf-8"))
        except Exception:
            st.session_state.cfg = {}
    else:
        st.session_state.cfg = {}


# --- GLOBAL CONSTANTS ---
sim_models = ["Normal Distribution (Bell Curve)", "Configurable Flat Return", "Historical Rolling Sequence (US S&P 500 1928-2023)", "Historical Rolling Sequence (UK FTSE All-Share 1986-2023)"]

drawdown_options = [
    "Tax-Optimised (Cap at Basic Rate, Protect Cash)",
    "Tax-Optimised (401k First, Cap at Basic Rate, Protect Cash)",
    "Dynamic Cash Buffer (Equities in Up years, Cash in Down years)",
    "Equities First (Preserve Cash completely)",
    "Cash First (Burn Cash immediately)",
    "Maximise Inheritance (Drain Pensions to preserve ISAs)"
]

dc_options = [
    "100% Taxable (Already taken / Default)",
    "UFPLS (25% Tax-Free per withdrawal)",
    "Phased PCLS (Move £20k/yr Tax-Free to S&S ISA)"
]


# --- Import & Export ---
with st.sidebar.expander("💾 Save & Load Scenarios", expanded=False):
    st.markdown("**Tip:** You can generate a URL to save or share your exact scenario.")
    if st.button("🔗 Generate Bookmark Link", help="Updates the URL with your current settings so you can bookmark or share it."):
        b64_str = base64.urlsafe_b64encode(json.dumps(st.session_state.cfg).encode("utf-8")).decode("utf-8").rstrip("=")
        st.query_params["state"] = b64_str
        st.success("URL updated! You can now bookmark or copy the URL.")
    
    st.write("---")
    st.write("Or, export/import to a file:")
    json_to_export = json.dumps(st.session_state.cfg, indent=2)
    st.download_button("📥 Export Settings (JSON)", data=json_to_export, file_name="retirement_scenario.json", mime="application/json")
    
    uploaded_file = st.file_uploader("📤 Import Settings (JSON)", type=["json"])
    if uploaded_file is not None:
        if st.button("Apply Imported Settings"):
            try:
                # Seek to 0 in case it was read before
                uploaded_file.seek(0)
                imported_cfg = json.load(uploaded_file)
                st.session_state.cfg = imported_cfg
                
                # Inject imported values directly into session state to force Streamlit to adopt them
                for k, v in imported_cfg.items():
                    if k == "drawdown_strategy_index":
                        st.session_state["drawdown_strategy_widget"] = drawdown_options[min(v, len(drawdown_options)-1)]
                    elif k == "sim_model_index":
                        st.session_state["sim_model_widget"] = sim_models[min(v, len(sim_models)-1)]
                    elif k == "dc_tax_free_index":
                        st.session_state["dc_tax_free_widget"] = dc_options[min(v, len(dc_options)-1)]
                    elif k == "spending_points":
                        widget_key = f"{k}_widget"
                        if widget_key in st.session_state:
                            del st.session_state[widget_key]
                    else:
                        widget_key = f"{k}_widget"
                        st.session_state[widget_key] = v
                
                # Do not set the massive URL on import to prevent websocket disconnects
                # Instead, clear any existing long URL to ensure stability
                st.query_params.clear()
                
                st.success("Settings imported successfully!")
            except Exception as e:
                st.error(f"Failed to load: {e}")


def get_val(key, default):
    return st.session_state.cfg.get(key, default)

# --- Tax Logic ---
# FIX #3: Corrected Scottish tax band widths to 2025-26 rates
# FIX #5: PA taper threshold (£100k) is NOT scaled with inflation — it has been frozen since 2010
def calculate_scottish_tax(gross, tax_inflation_factor=1.0):
    pa_taper_threshold = 100000  # Frozen — never uprated since 2010
    pa_base = 12570 * tax_inflation_factor
    
    pa = max(0, pa_base - max(0, (gross - pa_taper_threshold) / 2))
    taxable = max(0, gross - pa)
    
    tax = 0
    rem = taxable
    
    # 2025-26 Scottish Income Tax band widths (amount taxable at each rate)
    bands = [
        (2306 * tax_inflation_factor, 0.19),   # Starter rate
        (11685 * tax_inflation_factor, 0.20),   # Basic rate
        (17101 * tax_inflation_factor, 0.21),   # Intermediate rate
        (31338 * tax_inflation_factor, 0.42),   # Higher rate
        (50140 * tax_inflation_factor, 0.45),   # Advanced rate
    ]
    
    for limit, rate in bands:
        if rem > 0:
            amt = min(rem, limit)
            tax += amt * rate
            rem -= amt
            
    if rem > 0:
        tax += rem * 0.48  # Top rate
        
    return tax

# FIX #4: Increased binary search ceiling to handle PA tapering at high incomes
# FIX #10: Moved existing_net calculation outside the loop
def gross_up_for_tax_with_lsa(net_needed, existing_gross, tax_inflation_factor, lsa_remaining, is_ufpls=False):
    if net_needed <= 0: return 0.0, 0.0
    low = net_needed
    high = net_needed * 5  # Increased from 3x to 5x to handle PA tapering edge cases
    existing_net = existing_gross - calculate_scottish_tax(existing_gross, tax_inflation_factor)
    for _ in range(50):  # Increased iterations for better convergence
        mid = (low + high) / 2
        
        tf_part = min(mid * 0.25, lsa_remaining) if is_ufpls else 0.0
        taxable_part = mid - tf_part
        
        total_gross = existing_gross + taxable_part
        total_net = total_gross - calculate_scottish_tax(total_gross, tax_inflation_factor)
        actual_net = (total_net - existing_net) + tf_part
        
        if actual_net < net_needed:
            low = mid
        else:
            high = mid
            
    tf_used = min(high * 0.25, lsa_remaining) if is_ufpls else 0.0
    return high, tf_used

def gross_up_for_tax(net_needed, existing_gross, tax_inflation_factor=1.0):
    high, _ = gross_up_for_tax_with_lsa(net_needed, existing_gross, tax_inflation_factor, 0, False)
    return high

# --- Core Simulation ---
def simulate_scenario(inputs, market_returns, inflation_returns=None, cash_returns=None):
    years = inputs['end_age'] - inputs['current_age'] + 1
    age = inputs['current_age']
    
    # FIX #13: Guard against invalid age range
    if years <= 0:
        return pd.DataFrame()
    
    pots = {
        'Savings': inputs['savings_start'],
        'Cash_ISA': inputs['cash_isa_start'],
        'SS_ISA': inputs['ss_isa_start'],
        'GIA': inputs['gia_start'],
        'DC_Pension': inputs['dc_start'],
        'US_401k': inputs['k401_start_usd'] * inputs['usd_gbp_rate']
    }
    
    lsa_remaining = inputs.get('lsa_start', 268275)
    dc_tax_free_method = inputs.get('dc_tax_free_method', "100% Taxable (Already taken / Default)")
    gia_cost_basis = inputs['gia_start']
    
    results = []
    
    annuity_income_base = 0.0
    annuity_purchase_year = -1
    

    for i in range(years):
        if inflation_returns is not None:
            inflation_factor = np.prod(1 + inflation_returns[:i]) if i > 0 else 1.0
            
            tax_rates = inflation_returns * inputs.get('tax_band_match', 1.0)
            tax_inflation_factor = np.prod(1 + tax_rates[:i]) if i > 0 else 1.0
            
            db_cap = inputs.get('db_inflation', 0.025)
            db_rates = np.minimum(inflation_returns, db_cap)
            db_inflation_factor = np.prod(1 + db_rates[:i]) if i > 0 else 1.0
            
            sp_floor = inputs.get('sp_inflation', 0.025)
            sp_rates = np.maximum(inflation_returns, sp_floor)
            sp_inflation_factor = np.prod(1 + sp_rates[:i]) if i > 0 else 1.0
        else:
            inflation_factor = (1 + inputs['inflation_rate']) ** i
            tax_inflation_factor = (1 + inputs['tax_band_inflation']) ** i
            db_inflation_factor = (1 + inputs.get('db_inflation', inputs['inflation_rate'])) ** i
            sp_inflation_factor = (1 + inputs.get('sp_inflation', inputs['inflation_rate'])) ** i
        
        spending_points = inputs.get('spending_points', [])
        base_target_net = 50000 # Fallback
        if spending_points:
            valid_pts = []
            for pt in spending_points:
                try:
                    f_age = pt.get('From Age')
                    t_net = pt.get('Target Net (£)')
                    if f_age is not None and t_net is not None:
                        valid_pts.append({'From Age': int(f_age), 'Target Net (£)': float(t_net)})
                except (ValueError, TypeError):
                    pass
                    
            if valid_pts:
                sorted_pts = sorted(valid_pts, key=lambda x: x.get('From Age', 0), reverse=True)
                for pt in sorted_pts:
                    if age >= pt.get('From Age', 0):
                        base_target_net = pt.get('Target Net (£)', 0)
                        break
                # If age is before all points, use the first point chronologically (which is the last in reverse sort)
                if base_target_net == 0 and sorted_pts:
                    base_target_net = sorted_pts[-1].get('Target Net (£)', 0)

        target_net = base_target_net * inflation_factor
        
        # Check for annuity purchase
        if age == int(inputs.get('annuity_purchase_age', 75)) and inputs.get('annuity_purchase_pct', 0) > 0 and pots['DC_Pension'] > 0:
            pct = inputs['annuity_purchase_pct'] / 100.0
            amount_used = pots['DC_Pension'] * pct
            pots['DC_Pension'] -= amount_used
            annuity_income_base = amount_used * (inputs.get('annuity_rate_pct', 6.0) / 100.0)
            annuity_purchase_year = i
            
        current_annuity_income = 0.0
        if annuity_income_base > 0:
            if inputs.get('annuity_inflation_linked', False):
                if inflation_returns is not None:
                    ann_inf = np.prod(1 + inflation_returns[annuity_purchase_year:i]) if i > annuity_purchase_year else 1.0
                else:
                    ann_inf = (1 + inputs['inflation_rate']) ** (i - annuity_purchase_year) if i > annuity_purchase_year else 1.0
                current_annuity_income = annuity_income_base * ann_inf
            else:
                current_annuity_income = annuity_income_base
        
        db_income = inputs['db_pension'] * db_inflation_factor if age >= inputs['db_age'] else 0
        state_pension = inputs['state_pension'] * sp_inflation_factor if age >= inputs['state_pension_age'] else 0
        
        guaranteed_gross = db_income + state_pension + current_annuity_income
        total_guaranteed_tax = calculate_scottish_tax(guaranteed_gross, tax_inflation_factor)
        guaranteed_net = guaranteed_gross - total_guaranteed_tax
        
        if guaranteed_gross > 0:
            prop_state_db = (db_income + state_pension) / guaranteed_gross
            prop_annuity = current_annuity_income / guaranteed_gross
            db_state_net = guaranteed_net * prop_state_db
            annuity_net = guaranteed_net * prop_annuity
        else:
            db_state_net = 0.0
            annuity_net = 0.0
        
        shortfall_net = max(0, target_net - guaranteed_net)
        
        current_gross = guaranteed_gross
        
        # Check for DB lump sum event
        if age == inputs.get('db_age') and inputs.get('db_lump_sum', 0) > 0:
            db_ls = inputs['db_lump_sum']
            if lsa_remaining >= db_ls:
                lsa_remaining -= db_ls
                net_db_ls = db_ls
            else:
                taxable_db_ls = db_ls - lsa_remaining
                net_db_ls = lsa_remaining + (taxable_db_ls - (calculate_scottish_tax(current_gross + taxable_db_ls, tax_inflation_factor) - calculate_scottish_tax(current_gross, tax_inflation_factor)))
                current_gross += taxable_db_ls
                lsa_remaining = 0
            
            pots['Savings'] += net_db_ls
        
        withdrawals = {
            'Savings': 0, 'Cash_ISA': 0, 'SS_ISA': 0, 'GIA': 0, 'DC_Pension_Net': 0, 'US_401k_Net': 0
        }
        
        k401_available = age >= inputs['k401_access_age']
        
        ret = market_returns[i]
        if cash_returns is not None:
            cash_ret = cash_returns[i]
        else:
            cash_ret = inputs.get('cash_interest', 0.03)
            
        equity_alloc = inputs.get('equity_allocation', 1.0)
        
        # Blended return: (Equities * Equity_Alloc) + (Bonds/Cash * (1 - Equity_Alloc))
        portfolio_ret = (ret * equity_alloc) + (cash_ret * (1 - equity_alloc))
        
        # FIX #6: Apply growth BEFORE withdrawals
        # This models selling investments at year-end prices after growth/decline has occurred.
        
        # Savings interest: taxed at marginal rate with income-aware PSA
        savings_interest = pots['Savings'] * cash_ret
        higher_threshold = 43662 * tax_inflation_factor
        advanced_threshold = 75000 * tax_inflation_factor
        if guaranteed_gross > advanced_threshold:
            psa = 0    # Additional/Advanced rate: no PSA
        elif guaranteed_gross > higher_threshold:
            psa = 500  # Higher rate: £500 PSA
        else:
            psa = 1000 # Starter/Basic/Intermediate rate: £1,000 PSA
        taxable_interest = max(0, savings_interest - psa)
        if taxable_interest > 0:
            savings_tax = (calculate_scottish_tax(guaranteed_gross + taxable_interest, tax_inflation_factor)
                          - calculate_scottish_tax(guaranteed_gross, tax_inflation_factor))
        else:
            savings_tax = 0
        pots['Savings'] += savings_interest - savings_tax
        
        pots['Cash_ISA'] *= (1 + cash_ret)
        pots['SS_ISA'] *= (1 + portfolio_ret)
        
        # GIA: growth applied without CGT — CGT is only realised when selling
        pots['GIA'] *= (1 + portfolio_ret)
        # Cost basis unchanged by growth (unrealised gains)
        cgt_allowance_remaining = 3000  # Annual CGT exemption (2024-25 onwards)
        cgt_paid_this_year = 0
        
        pots['DC_Pension'] *= (1 + portfolio_ret)
        pots['US_401k'] *= (1 + portfolio_ret)
        
        # FIX #1 & #2: Clamp all pots to zero — prevents negative balances from compounding
        for pot_key in pots:
            pots[pot_key] = max(0.0, pots[pot_key])
            
        # A. Phased PCLS Bed & ISA Transfer
        if dc_tax_free_method == "Phased PCLS (Move £20k/yr Tax-Free to S&S ISA)":
            # To extract PCLS, you must crystallise 4x the amount. 
            max_transfer = min(20000, lsa_remaining, pots['DC_Pension'] * 0.25)
            if max_transfer > 0:
                pots['DC_Pension'] -= max_transfer
                pots['SS_ISA'] += max_transfer
                lsa_remaining -= max_transfer
        
        # B. Fill 0% Personal Allowance from taxable pensions
        current_pa = 12570 * tax_inflation_factor
        unused_pa = max(0, current_pa - current_gross)
        strategy = inputs.get('drawdown_strategy', "Dynamic Cash Buffer (Equities in Up years, Cash in Down years)")
        
        if unused_pa > 0 and shortfall_net > 0:
            is_401k_first = (strategy == "Tax-Optimised (401k First, Cap at Basic Rate, Protect Cash)")
            
            def fill_pa_401k():
                nonlocal unused_pa, shortfall_net, current_gross
                if k401_available and unused_pa > 0 and shortfall_net > 0:
                    take_401k = min(pots['US_401k'], unused_pa, shortfall_net)
                    pots['US_401k'] -= take_401k
                    shortfall_net -= take_401k
                    current_gross += take_401k
                    unused_pa -= take_401k
                    withdrawals['US_401k_Net'] += take_401k

            def fill_pa_dc():
                nonlocal unused_pa, shortfall_net, current_gross, lsa_remaining
                if unused_pa > 0 and shortfall_net > 0:
                    if dc_tax_free_method == "25% Tax-Free (UFPLS)":
                        if (unused_pa / 3) <= lsa_remaining:
                            desired_w = unused_pa / 0.75
                        else:
                            desired_w = unused_pa + lsa_remaining
                    else:
                        desired_w = unused_pa
                        
                    take_dc_gross = min(pots['DC_Pension'], desired_w, shortfall_net)
                    
                    if dc_tax_free_method == "25% Tax-Free (UFPLS)":
                        tf_used = min(take_dc_gross * 0.25, lsa_remaining)
                        taxable_part = take_dc_gross - tf_used
                        lsa_remaining -= tf_used
                    else:
                        taxable_part = take_dc_gross
                        
                    pots['DC_Pension'] -= take_dc_gross
                    shortfall_net -= take_dc_gross
                    current_gross += taxable_part
                    unused_pa -= taxable_part
                    withdrawals['DC_Pension_Net'] += take_dc_gross

            if is_401k_first:
                fill_pa_401k()
                fill_pa_dc()
            else:
                fill_pa_dc()
                fill_pa_401k()
        
        is_downturn = portfolio_ret < 0.0
        
        def execute_drawdown(pot_name, tax_type, max_net_take=None, min_pot_balance=0.0):
            nonlocal shortfall_net, current_gross, lsa_remaining, gia_cost_basis, cgt_allowance_remaining, cgt_paid_this_year
            take_target = shortfall_net if max_net_take is None else min(shortfall_net, max_net_take)
            if take_target <= 0: return
            
            available_pot = max(0.0, pots[pot_name] - min_pot_balance)
            if available_pot <= 0: return
            if pot_name == 'US_401k' and not k401_available: return

            if tax_type == 'tax_free':
                take = min(available_pot, take_target)
                pots[pot_name] -= take
                shortfall_net -= take
                withdrawals[pot_name] += take
            elif tax_type == 'gia':
                # CGT on proportional gain only when selling
                pot_val = pots[pot_name]
                if pot_val > 0 and gia_cost_basis < pot_val:
                    gain_pct = 1 - (gia_cost_basis / pot_val)
                else:
                    gain_pct = 0.0
                
                # Calculate gross withdrawal needed to achieve net target
                if gain_pct > 0:
                    test_gain = take_target * gain_pct
                    if test_gain > cgt_allowance_remaining:
                        allowance_benefit = cgt_allowance_remaining * 0.20
                        effective_rate = 0.20 * gain_pct
                        gross_needed = (take_target - allowance_benefit) / (1 - effective_rate)
                    else:
                        gross_needed = take_target  # All gains within allowance
                else:
                    gross_needed = take_target  # No gains
                
                take = min(available_pot, gross_needed)
                gain_on_sale = take * gain_pct
                taxable_gain = max(0, gain_on_sale - cgt_allowance_remaining)
                cgt_on_sale = taxable_gain * 0.20
                actual_net = take - cgt_on_sale
                
                # Update tracking
                cgt_allowance_remaining = max(0, cgt_allowance_remaining - gain_on_sale)
                cgt_paid_this_year += cgt_on_sale
                old_pot = pot_val
                pots[pot_name] -= take
                if old_pot > 0:
                    gia_cost_basis *= (pots[pot_name] / old_pot)
                else:
                    gia_cost_basis = 0
                
                shortfall_net -= actual_net
                withdrawals[pot_name] += actual_net
            elif tax_type == 'taxable':
                is_dc = (pot_name == 'DC_Pension')
                is_ufpls = (is_dc and dc_tax_free_method == "25% Tax-Free (UFPLS)")
                
                gross_needed, tf_used = gross_up_for_tax_with_lsa(
                    take_target, current_gross, tax_inflation_factor, lsa_remaining, is_ufpls
                )
                take_gross = min(available_pot, gross_needed)
                
                if take_gross < gross_needed:
                    actual_tf = min(take_gross * 0.25, lsa_remaining) if is_ufpls else 0.0
                    actual_taxable = take_gross - actual_tf
                    tax_added = calculate_scottish_tax(current_gross + actual_taxable, tax_inflation_factor) - calculate_scottish_tax(current_gross, tax_inflation_factor)
                    actual_net = take_gross - tax_added
                    if is_ufpls: lsa_remaining -= actual_tf
                else:
                    actual_net = take_target
                    actual_taxable = take_gross - tf_used
                    if is_ufpls: lsa_remaining -= tf_used
                
                pots[pot_name] -= take_gross
                shortfall_net -= actual_net
                current_gross += actual_taxable
                withdrawals[f"{pot_name}_Net"] += actual_net

        if strategy in ["Tax-Optimised (Cap at Basic Rate, Protect Cash)", "Tax-Optimised (401k First, Cap at Basic Rate, Protect Cash)"]:
            def get_net_headroom(is_ufpls_pot=False):
                limit_gross = 43662 * tax_inflation_factor
                if current_gross >= limit_gross: return 0.0
                net_at_limit = limit_gross - calculate_scottish_tax(limit_gross, tax_inflation_factor)
                current_net = current_gross - calculate_scottish_tax(current_gross, tax_inflation_factor)
                headroom = max(0.0, net_at_limit - current_net)
                if is_ufpls_pot and lsa_remaining > 0:
                    taxable_gross_room = max(0.0, limit_gross - current_gross)
                    tf_bonus = min(taxable_gross_room / 3, lsa_remaining)
                    return headroom + tf_bonus
                return headroom

            target_buffer = inputs.get('cash_buffer_years', 2) * target_net
            
            if strategy == "Tax-Optimised (401k First, Cap at Basic Rate, Protect Cash)":
                pension_order = [('US_401k', 'taxable'), ('DC_Pension', 'taxable')]
            else:
                pension_order = [('DC_Pension', 'taxable'), ('US_401k', 'taxable')]
            
            if is_downturn:
                # DOWNTURN: Avoid selling equities. Drain cash first down to the buffer limit.
                execute_drawdown('Cash_ISA', 'tax_free', min_pot_balance=target_buffer)
                execute_drawdown('Savings', 'tax_free', min_pot_balance=max(0.0, target_buffer - pots['Cash_ISA']))
                
                # If still short, sell equities but cap at Basic Rate to avoid 42%
                for pot_name, tax_type in pension_order:
                    is_ufpls_pot = (pot_name == 'DC_Pension' and dc_tax_free_method == "25% Tax-Free (UFPLS)")
                    execute_drawdown(pot_name, tax_type, max_net_take=get_net_headroom(is_ufpls_pot))
                execute_drawdown('SS_ISA', 'tax_free')
                execute_drawdown('GIA', 'gia')
                
                # If still short, break into 42% tax bracket
                for pot_name, tax_type in pension_order:
                    execute_drawdown(pot_name, tax_type)
                
                # Ultimate emergency: break the cash buffer
                execute_drawdown('Cash_ISA', 'tax_free')
                execute_drawdown('Savings', 'tax_free')
            else:
                # UP YEAR: Sell equities. Cap at Basic Rate to avoid 42%
                for pot_name, tax_type in pension_order:
                    is_ufpls_pot = (pot_name == 'DC_Pension' and dc_tax_free_method == "25% Tax-Free (UFPLS)")
                    execute_drawdown(pot_name, tax_type, max_net_take=get_net_headroom(is_ufpls_pot))
                
                # Fill remaining shortfall from tax-free equity accounts
                execute_drawdown('SS_ISA', 'tax_free')
                execute_drawdown('GIA', 'gia')
                
                # If still short, dip into cash (but protect buffer)
                execute_drawdown('Cash_ISA', 'tax_free', min_pot_balance=target_buffer)
                execute_drawdown('Savings', 'tax_free', min_pot_balance=max(0.0, target_buffer - pots['Cash_ISA']))
                
                # If still short, break into 42% tax bracket
                for pot_name, tax_type in pension_order:
                    execute_drawdown(pot_name, tax_type)
                
                # Ultimate emergency: break the cash buffer
                execute_drawdown('Cash_ISA', 'tax_free')
                execute_drawdown('Savings', 'tax_free')
        else:
            if strategy == "Dynamic Cash Buffer (Equities in Up years, Cash in Down years)":
                if not is_downturn:
                    draw_order = [
                        ('US_401k', 'taxable'), ('SS_ISA', 'tax_free'), ('GIA', 'gia'),
                        ('DC_Pension', 'taxable'), ('Savings', 'tax_free'), ('Cash_ISA', 'tax_free')
                    ]
                else:
                    draw_order = [
                        ('Savings', 'tax_free'), ('Cash_ISA', 'tax_free'), ('US_401k', 'taxable'),
                        ('SS_ISA', 'tax_free'), ('GIA', 'gia'), ('DC_Pension', 'taxable')
                    ]
            elif strategy == "Equities First (Preserve Cash completely)":
                draw_order = [
                    ('US_401k', 'taxable'), ('SS_ISA', 'tax_free'), ('GIA', 'gia'),
                    ('DC_Pension', 'taxable'), ('Savings', 'tax_free'), ('Cash_ISA', 'tax_free')
                ]
            elif strategy == "Cash First (Burn Cash immediately)":
                draw_order = [
                    ('Savings', 'tax_free'), ('Cash_ISA', 'tax_free'), ('DC_Pension', 'taxable'),
                    ('US_401k', 'taxable'), ('SS_ISA', 'tax_free'), ('GIA', 'gia')
                ]
            elif strategy == "Maximise Inheritance (Drain Pensions to preserve ISAs)":
                draw_order = [
                    ('DC_Pension', 'taxable'), ('US_401k', 'taxable'), ('Savings', 'tax_free'),
                    ('Cash_ISA', 'tax_free'), ('GIA', 'gia'), ('SS_ISA', 'tax_free')
                ]
            else:
                # Default fallback
                draw_order = [
                    ('DC_Pension', 'taxable'), ('US_401k', 'taxable'), ('Savings', 'tax_free'),
                    ('Cash_ISA', 'tax_free'), ('GIA', 'gia'), ('SS_ISA', 'tax_free')
                ]
                
            for pot_name, tax_type in draw_order:
                execute_drawdown(pot_name, tax_type)
        
        total_pot = sum(max(0, v) for v in pots.values())
        total_net_achieved = guaranteed_net + sum(withdrawals.values())
        
        results.append({
            'Age': age,
            'Stock Market Return': ret,
            'Blended Portfolio Return': portfolio_ret,
            'Target Net Income': target_net,
            'Total Net Achieved': total_net_achieved,
            'Unfunded Shortfall': shortfall_net,
            'Funded From: Guaranteed': db_state_net,
            'Funded From: Annuity': annuity_net,
            'Funded From: Savings': withdrawals['Savings'],
            'Funded From: Cash ISA': withdrawals['Cash_ISA'],
            'Funded From: S&S ISA': withdrawals['SS_ISA'],
            'Funded From: GIA': withdrawals['GIA'],
            'Funded From: 401k (Net)': withdrawals['US_401k_Net'],
            'Funded From: DC Pension (Net)': withdrawals['DC_Pension_Net'],
            'Total Tax Paid': calculate_scottish_tax(current_gross, tax_inflation_factor) + savings_tax + cgt_paid_this_year,
            'Savings Balance': pots['Savings'],
            'Cash ISA Balance': pots['Cash_ISA'],
            'S&S ISA Balance': pots['SS_ISA'],
            'GIA Balance': pots['GIA'],
            '401k Balance (GBP)': pots['US_401k'],
            'DC Pension Balance': pots['DC_Pension'],
            'Total Pot': total_pot,
            'Total Pot Real': total_pot / inflation_factor if inflation_factor > 0 else 0,
            'Inflation Factor': inflation_factor,
            'Cash Yield': cash_ret
        })
        
        age += 1
        
    return pd.DataFrame(results)

# --- UI ---
st.title("Retirement Simulator (Scottish Tax)")
tab_sim, tab_help = st.tabs(["📊 Simulator", "📖 Documentation & Help"])

with st.sidebar:
    st.title("Configuration")
    
    with st.expander("1. Personal Details", expanded=False):
        current_age = st.number_input("Current Age", min_value=18, max_value=99, value=get_val('current_age', 55), key="current_age_widget", help="Your current age.")
        end_age = st.number_input("End Age (Life Expectancy)", min_value=50, max_value=120, value=get_val('end_age', 95), key="end_age_widget", help="The age you expect to live until. Used to calculate the total simulation length.")
        
    with st.expander("2. Income Goals", expanded=False):
        st.write("Target Net Income (Today's £) by Age")
        st.caption("Define your spending phases. The simulator automatically inflates these targets over time. Add as many phases as you like.")
        default_spending = [{"From Age": 55, "Target Net (£)": 50000}, {"From Age": 75, "Target Net (£)": 35000}]
        spending_points = st.data_editor(
            get_val('spending_points', default_spending), 
            num_rows="dynamic", 
            key="spending_points_widget", 
            use_container_width=True, 
            hide_index=True,
            column_config={
                "From Age": st.column_config.NumberColumn("From Age", min_value=18, max_value=120, step=1, required=True),
                "Target Net (£)": st.column_config.NumberColumn("Target Net (£)", min_value=0, step=1000, format="£%d", required=True)
            }
        )
        
        if spending_points:
            try:
                first_target = None
                for pt in spending_points:
                    if pt.get("Target Net (£)") is not None:
                        first_target = float(pt.get("Target Net (£)"))
                        break
                        
                if first_target is not None:
                    gross_first = gross_up_for_tax(first_target, 0, 1.0)
                    st.markdown(f"<p style='margin-top:-5px; margin-bottom:15px; font-size:14px; color:gray;'>First phase equivalent to Gross Salary: <b>£{gross_first:,.0f}</b> <br/><i>(Note: Retirees do not pay National Insurance on pension income)</i></p>", unsafe_allow_html=True)
            except Exception:
                pass
        
        
    with st.expander("3. Guaranteed Income", expanded=False):
        state_pension = st.number_input("UK State Pension (Annual £)", min_value=0.0, value=float(get_val('state_pension', 12547.60)), step=100.0, key="state_pension_widget", help="Current value of the full UK State Pension.")
        state_pension_age = st.number_input("State Pension Age", min_value=55, max_value=80, value=get_val('state_pension_age', 67), key="state_pension_age_widget", help="The age you will start receiving the UK State Pension.")
        db_pension = st.number_input("Defined Benefit Pension (Annual £)", min_value=0, value=get_val('db_pension', 0), key="db_pension_widget", help="Annual gross income from your Defined Benefit (final salary) pension.")
        db_age = st.number_input("DB Pension Start Age", min_value=50, max_value=80, value=get_val('db_age', 65), key="db_age_widget", help="The age your DB pension begins paying out.")
        db_lump_sum = st.number_input("DB Tax-Free Lump Sum (PCLS) (£)", min_value=0, value=get_val('db_lump_sum', 0), step=5000, key="db_lump_sum_widget", help="Tax-free lump sum received at DB start age. Uses up your Lump Sum Allowance.")
        
        st.write("---")
        _sim_model_val = st.session_state.get('sim_model_widget')
        if _sim_model_val is None:
            _sim_model_idx = get_val('sim_model_index', 0)
            _sim_model_val = sim_models[_sim_model_idx] if _sim_model_idx < len(sim_models) else sim_models[0]
            
        _hist_checkbox_val = st.session_state.get('use_hist_inflation_widget')
        if _hist_checkbox_val is None:
            _hist_checkbox_val = get_val('use_hist_inflation', False)
            
        _is_hist_inf = ("Historical Rolling Sequence" in _sim_model_val) and _hist_checkbox_val
        
        if _is_hist_inf:
            state_pension_inflation_pct = st.slider("State Pension Inflation Floor (%)", 0.0, 10.0, get_val('state_pension_inflation_pct', get_val('inflation_rate_pct', 2.5)), 0.1, key="state_pension_inflation_pct_widget", help="Because Historical UK Inflation is enabled, the State Pension will perfectly mirror historical inflation rates, but this slider acts as the absolute floor (the Triple Lock guarantee).")
            db_pension_inflation_pct = st.slider("DB Pension Inflation Cap (%)", 0.0, 10.0, get_val('db_pension_inflation_pct', min(get_val('inflation_rate_pct', 2.5), 2.5)), 0.1, key="db_pension_inflation_pct_widget", help="Because Historical UK Inflation is enabled, the DB Pension will perfectly mirror historical inflation rates, but this slider acts as the absolute cap.")
        else:
            state_pension_inflation_pct = st.slider("State Pension Inflation (%)", 0.0, 10.0, get_val('state_pension_inflation_pct', get_val('inflation_rate_pct', 2.5)), 0.1, key="state_pension_inflation_pct_widget", help="Annual increase in the UK State Pension. The Triple Lock guarantees the higher of inflation, average earnings growth, or 2.5%. Defaults to general inflation (conservative).")
            db_pension_inflation_pct = st.slider("DB Pension Inflation (%)", 0.0, 10.0, get_val('db_pension_inflation_pct', min(get_val('inflation_rate_pct', 2.5), 2.5)), 0.1, key="db_pension_inflation_pct_widget", help="Annual increase in your Defined Benefit pension. Many schemes cap increases at CPI or 2.5%, whichever is lower. Defaults to min(inflation, 2.5%).")
        
    with st.expander("4. Pensions & Allowances", expanded=False):
        dc_start = st.number_input("Defined Contribution Pension (£)", min_value=0, value=get_val('dc_start', 0), step=10000, key="dc_start_widget", help="Defined Contribution Pension. Handled according to the strategy chosen below.")
        
        st.write("---")
        st.markdown("**Annuity Purchase (from DC Pension)**")
        _curr_pct = st.session_state.get('annuity_purchase_pct_widget')
        _pct_val = _curr_pct if _curr_pct is not None else get_val('annuity_purchase_pct', 0)
        annuity_purchase_pct = st.slider("Percentage of DC Pension to Convert to Annuity (%)", 0, 100, _pct_val, 5, key="annuity_purchase_pct_widget", help="What percentage of your DC Pension pot will be used to buy a lifetime annuity?")
        
        _curr_age = st.session_state.get('annuity_purchase_age_widget')
        _age_val = _curr_age if _curr_age is not None else get_val('annuity_purchase_age', 75)
        annuity_purchase_age = st.number_input("Annuity Purchase Age", min_value=50, max_value=99, value=int(_age_val), key="annuity_purchase_age_widget", help="Age at which the annuity is purchased.")
        
        _curr_rate = st.session_state.get('annuity_rate_pct_widget')
        _rate_val = _curr_rate if _curr_rate is not None else get_val('annuity_rate_pct', 6.0)
        annuity_rate_pct = st.slider("Annuity Rate (%)", 1.0, 15.0, float(_rate_val), 0.1, key="annuity_rate_pct_widget", help="The guaranteed annual payout rate (e.g. 6% means £6k a year per £100k purchased).")
        
        _curr_link = st.session_state.get('annuity_inflation_linked_widget')
        _link_val = _curr_link if _curr_link is not None else get_val('annuity_inflation_linked', False)
        annuity_inflation_linked = st.checkbox("Annuity is Inflation Linked", value=bool(_link_val), key="annuity_inflation_linked_widget", help="Does the annuity payout rise with inflation every year?")
        st.write("---")
        
        dc_options = [
            "100% Taxable (Already taken / Default)",
            "25% Tax-Free (UFPLS) on every withdrawal",
            "Phased PCLS (Move £20k/yr Tax-Free to S&S ISA)"
        ]
        _current_dc_val = st.session_state.get("dc_tax_free_widget")
        if _current_dc_val is not None and _current_dc_val in dc_options:
            saved_dc_index = dc_options.index(_current_dc_val)
        else:
            saved_dc_index = get_val('dc_tax_free_index', 0)
            if saved_dc_index >= len(dc_options): saved_dc_index = 0
        
        dc_tax_free_method = st.radio("DC Pension Tax-Free Handling", dc_options, index=saved_dc_index, key="dc_tax_free_widget", help="UFPLS makes every withdrawal 25% tax-free. Phased PCLS transfers £20k a year into your ISA until the allowance runs out. 100% Taxable assumes you've already taken the cash.")
        lsa_start = st.number_input("Lump Sum Allowance (LSA) Remaining (£)", min_value=0, max_value=268275, value=get_val('lsa_start', 268275), step=1000, key="lsa_start_widget", help="The UK LSA limits how much tax-free cash you can take across your lifetime. Max is £268,275.")
        
        st.write("---")
        k401_start_usd = st.number_input("US 401k ($ USD)", min_value=0, value=get_val('k401_start_usd', 0), step=10000, key="k401_start_usd_widget", help="US 401k balance in USD. Treated as 100% taxable in the UK.")
        k401_access_age = st.number_input("US 401k Access Age (Penalty Free)", min_value=50.0, max_value=70.0, value=float(get_val('k401_access_age', 59.5)), step=0.5, key="k401_access_age_widget", help="Age at which you can access your 401k without a 10% penalty (typically 59.5).")

    with st.expander("5. Savings & Investments", expanded=False):
        savings_start = st.number_input("Cash Savings (£)", min_value=0, value=get_val('savings_start', 0), step=5000, key="savings_start_widget", help="Cash in regular bank accounts. Interest taxed above the £500 Personal Savings Allowance.")
        cash_isa_start = st.number_input("Cash ISA (£)", min_value=0, value=get_val('cash_isa_start', 0), step=5000, key="cash_isa_start_widget", help="Cash in a tax-free ISA.")
        ss_isa_start = st.number_input("Stocks & Shares ISA (£)", min_value=0, value=get_val('ss_isa_start', 0), step=10000, key="ss_isa_start_widget", help="Investments in a tax-free Stocks & Shares ISA.")
        gia_start = st.number_input("General Investment Account (£)", min_value=0, value=get_val('gia_start', 0), step=5000, key="gia_start_widget", help="General Investment Account. Capital gains taxed at 20% above the £3,000 annual CGT exemption.")
        
    with st.expander("6. Market Assumptions", expanded=False):
        _current_sim_val = st.session_state.get("sim_model_widget")
        if _current_sim_val is not None and _current_sim_val in sim_models:
            sim_model_index = sim_models.index(_current_sim_val)
        else:
            sim_model_index = get_val('sim_model_index', 0)
            if sim_model_index >= len(sim_models): sim_model_index = 0
            
        sim_model = st.radio("Simulation Model", sim_models, index=sim_model_index, key="sim_model_widget", help="Bell Curve uses random math. Historical Rolling uses real sequence returns.")
        
        equity_allocation_pct = st.slider("Portfolio Equity Allocation (Stocks vs Bonds/Cash)", 0.0, 100.0, get_val('equity_allocation_pct', 100.0), 5.0, key="equity_allocation_pct_widget", help="100% means fully invested in stocks. 60% means 60% stocks and 40% bonds/cash.")
        
        market_mean_pct = get_val('market_mean_pct', 5.0)
        market_vol_pct = get_val('market_vol_pct', 12.0)
        
        if sim_model == "Normal Distribution (Bell Curve)":
            market_mean_pct = st.slider("Expected Stock Market Return (%)", 0.0, 15.0, market_mean_pct, 0.1, key="market_mean_pct_widget", help="Expected average annual return of the stock market.")
            market_vol_pct = st.slider("Stock Market Volatility (%)", 0.0, 30.0, market_vol_pct, 0.5, key="market_vol_pct_widget", help="Expected volatility (standard deviation) of the stock market.")
            mc_sim_count = st.number_input("Monte Carlo Simulations", min_value=1, max_value=1000, value=get_val('mc_sim_count', 500), key="mc_sim_count_widget", help="Number of random lifetimes to simulate.")
            sim_count = mc_sim_count
        elif sim_model == "Configurable Flat Return":
            market_mean_pct = st.slider("Flat Stock Market Return (%)", 0.0, 15.0, market_mean_pct, 0.1, key="market_mean_pct_widget", help="Expected flat annual return of the stock market.")
            market_vol_pct = 0.0
            mc_sim_count = get_val('mc_sim_count', 500)
            sim_count = 1
        elif sim_model == "Historical Rolling Sequence (US S&P 500 1928-2023)":
            st.info("Using exactly 96 retirements, each starting in a different historical year (1928 to 2023). Stock Return sliders disabled.")
            mc_sim_count = get_val('mc_sim_count', 500)
            sim_count = 96
        else:
            st.info("Using exactly 38 retirements, each starting in a different historical year (1986 to 2023). Stock Return sliders disabled.")
            mc_sim_count = get_val('mc_sim_count', 500)
            sim_count = 38
            
        market_mean = market_mean_pct / 100.0
        market_vol = market_vol_pct / 100.0
        
        st.write("---")
        is_historical = "Historical Rolling Sequence" in sim_model
        if is_historical:
            use_hist_inflation = st.checkbox("Use Historical UK Inflation & Interest Rates", value=get_val('use_hist_inflation', False), key="use_hist_inflation_widget", help="Links inflation and Bank of England base rates directly to the historical year being simulated.")
        else:
            use_hist_inflation = False
            
        if use_hist_inflation:
            st.info("Inflation and Cash/Bond Yields will perfectly mirror historical UK rates for the simulated years.")
            inflation_rate_pct = get_val('inflation_rate_pct', 2.5)
            inflation_rate = inflation_rate_pct / 100.0
            _curr_match = st.session_state.get("tax_band_match_pct_widget")
            fallback_match_pct = get_val('tax_band_match', 1.0) * 100.0
            saved_match_pct = get_val('tax_band_match_pct', fallback_match_pct)
            display_match_pct = float(_curr_match) if _curr_match is not None else saved_match_pct
            tax_band_match_pct = st.slider("Tax Bracket Inflation Match (%)", 0.0, 150.0, display_match_pct, 5.0, key="tax_band_match_pct_widget", help="If 100%, Scottish tax brackets rise exactly with historical inflation. If 50%, they only rise by half the inflation amount (creating fiscal drag).")
            tax_band_match = tax_band_match_pct / 100.0
            tax_band_inflation = inflation_rate * tax_band_match
            tax_band_inflation_pct = tax_band_inflation * 100.0
            
            cash_interest_pct = get_val('cash_interest_pct', 3.0)
            cash_interest = cash_interest_pct / 100.0
        else:
            inflation_rate_pct = st.slider("General Inflation Rate (%)", 0.0, 10.0, get_val('inflation_rate_pct', 2.5), 0.1, key="inflation_rate_pct_widget", help="Assumed annual increase in the cost of goods and services.")
            inflation_rate = inflation_rate_pct / 100.0
            _curr_band_inf = st.session_state.get("tax_band_inflation_pct_widget")
            saved_band_inf = get_val('tax_band_inflation_pct', 0.0)
            display_band_inf = float(_curr_band_inf) if _curr_band_inf is not None else saved_band_inf
            tax_band_inflation_pct = st.slider("Tax Bracket Inflation (%) (Fiscal Drag)", 0.0, 10.0, display_band_inf, 0.1, key="tax_band_inflation_pct_widget", help="Assumed annual increase in Scottish tax thresholds. If this is lower than inflation, you will suffer 'fiscal drag' and pay more tax over time. Note: the £100k PA taper threshold is always frozen.")
            tax_band_inflation = tax_band_inflation_pct / 100.0
            fallback_match_pct = get_val('tax_band_match', 1.0) * 100.0
            tax_band_match_pct = get_val('tax_band_match_pct', fallback_match_pct)
            tax_band_match = 1.0
            
            cash_interest_pct = st.slider("Cash / Bond Yield Rate (%)", 0.0, 10.0, get_val('cash_interest_pct', 3.0), 0.1, key="cash_interest_pct_widget", help="Yield rate applied to Cash Savings, Cash ISAs, and the non-equity portion of your investment portfolio.")
            cash_interest = cash_interest_pct / 100.0
            
        st.write("---")
        usd_gbp_rate = st.number_input("USD to GBP Exchange Rate", min_value=0.01, max_value=5.0, value=get_val('usd_gbp_rate', 0.75), step=0.01, key="usd_gbp_rate_widget", help="Exchange rate for converting your US 401k to GBP.")
        
    with st.expander("7. Drawdown Strategy", expanded=False):
        _current_dd_val = st.session_state.get("drawdown_strategy_widget")
        if _current_dd_val is not None and _current_dd_val in drawdown_options:
            saved_index = drawdown_options.index(_current_dd_val)
        else:
            saved_index = get_val('drawdown_strategy_index', 0)
            if saved_index >= len(drawdown_options): saved_index = 0
        
        drawdown_strategy = st.radio("Drawdown Strategy", drawdown_options, index=saved_index, key="drawdown_strategy_widget", help="Tax-Optimised mathematically blends pots to avoid the 42% tax bracket. Other options rigidly follow a sequence.")
        
        cash_buffer_years = get_val('cash_buffer_years', 2)
        if drawdown_strategy in ["Tax-Optimised (Cap at Basic Rate, Protect Cash)", "Tax-Optimised (401k First, Cap at Basic Rate, Protect Cash)"]:
            cash_buffer_years = st.slider("Cash Buffer (Years of Income)", 0, 10, cash_buffer_years, 1, key="cash_buffer_years_widget", help="How many years of income to protect in Cash Savings / Cash ISA before being forced into the 42% tax bracket.")
            
    st.markdown("<br><br><br><br><br><br><br><br>", unsafe_allow_html=True)

st.session_state.cfg = {
    'current_age': current_age,
    'end_age': end_age,
    'spending_points': spending_points,
    'inflation_rate_pct': inflation_rate_pct,
    'tax_band_inflation_pct': tax_band_inflation_pct,
    'usd_gbp_rate': usd_gbp_rate,
    'state_pension': state_pension,
    'state_pension_age': state_pension_age,
    'db_pension': db_pension,
    'db_age': db_age,
    'db_lump_sum': db_lump_sum,
    'use_hist_inflation': use_hist_inflation,
    'tax_band_match': tax_band_match,
    'tax_band_match_pct': tax_band_match_pct,
    'state_pension_inflation_pct': state_pension_inflation_pct,
    'db_pension_inflation_pct': db_pension_inflation_pct,
    'savings_start': savings_start,
    'cash_isa_start': cash_isa_start,
    'ss_isa_start': ss_isa_start,
    'gia_start': gia_start,
    'dc_start': dc_start,
    'annuity_purchase_pct': annuity_purchase_pct,
    'annuity_purchase_age': annuity_purchase_age,
    'annuity_rate_pct': annuity_rate_pct,
    'annuity_inflation_linked': annuity_inflation_linked,
    'k401_start_usd': k401_start_usd,
    'k401_access_age': k401_access_age,
    'sim_model_index': sim_models.index(sim_model),
    'equity_allocation_pct': equity_allocation_pct,
    'market_mean_pct': market_mean_pct,
    'market_vol_pct': market_vol_pct,
    'cash_interest_pct': cash_interest_pct,
    'mc_sim_count': mc_sim_count,
    'drawdown_strategy_index': drawdown_options.index(drawdown_strategy),
    'dc_tax_free_index': dc_options.index(dc_tax_free_method),
    'lsa_start': lsa_start,
    'cash_buffer_years': cash_buffer_years
}






# FIX #13: Validate age range
if current_age >= end_age:
    st.error("Current Age must be less than End Age (Life Expectancy).")
    st.stop()

inputs = {
    'current_age': current_age,
    'end_age': end_age,
    'spending_points': spending_points,
    'inflation_rate': inflation_rate,
    'tax_band_inflation': tax_band_inflation,
    'usd_gbp_rate': usd_gbp_rate,
    'state_pension': state_pension,
    'state_pension_age': state_pension_age,
    'db_pension': db_pension,
    'db_age': db_age,
    'db_lump_sum': db_lump_sum,
    'use_hist_inflation': use_hist_inflation,
    'tax_band_match': tax_band_match,
    'sp_inflation': state_pension_inflation_pct / 100.0,
    'db_inflation': db_pension_inflation_pct / 100.0,
    'savings_start': savings_start,
    'cash_isa_start': cash_isa_start,
    'ss_isa_start': ss_isa_start,
    'gia_start': gia_start,
    'dc_start': dc_start,
    'k401_start_usd': k401_start_usd,
    'k401_access_age': k401_access_age,
    'equity_allocation': equity_allocation_pct / 100.0,
    'cash_interest': cash_interest,
    'drawdown_strategy': drawdown_strategy,
    'dc_tax_free_method': dc_tax_free_method,
    'lsa_start': lsa_start,
    'cash_buffer_years': cash_buffer_years
}

with tab_sim:
    with st.spinner(f"Running {sim_count} simulations..."):
        all_results = []
        years = end_age - current_age + 1
        
        HISTORICAL_RETURNS_SP500 = np.array([
            0.4381, -0.0830, -0.2512, -0.4384, -0.0864, 0.4998, -0.0119, 0.4674, 0.3194, -0.3534,
            0.2928, -0.0110, -0.1067, -0.1277, 0.1917, 0.2506, 0.1903, 0.3582, -0.1187, 0.0520,
            0.0570, 0.1830, 0.3081, 0.2368, 0.1815, -0.0121, 0.5256, 0.3260, 0.0744, -0.1046,
            0.4372, 0.1206, 0.0034, 0.2664, -0.0881, 0.2261, 0.1642, 0.1242, -0.0997, 0.2368,
            0.1081, -0.0824, 0.0356, 0.1422, 0.1876, -0.1431, -0.2590, 0.3700, 0.2383, -0.0698,
            0.0656, 0.1844, 0.3250, -0.0491, 0.2155, 0.2256, 0.0627, 0.3173, 0.1867, 0.0525,
            0.1661, 0.3169, -0.0310, 0.3047, 0.0762, 0.1008, 0.0132, 0.3758, 0.2296, 0.3336,
            0.2858, 0.2104, -0.0910, -0.1189, -0.2210, 0.2868, 0.1088, 0.0491, 0.1579, 0.0549,
            -0.3700, 0.2646, 0.1506, 0.0211, 0.1600, 0.3239, 0.1369, 0.0138, 0.1196, 0.2183,
            -0.0438, 0.3149, 0.1840, 0.2871, -0.1811, 0.2629
        ])
        
        HISTORICAL_RETURNS_FTSE = np.array([
            0.2563, 0.0799, 0.0998, 0.3351, -0.1081, 0.1856, 0.1833, 0.2684, -0.0605, 0.2201, 
            0.1518, 0.2323, 0.1441, 0.2475, -0.0447, -0.1191, -0.2147, 0.2007, 0.1271, 0.216, 
            0.1665, 0.0553, -0.2928, 0.2846, 0.1444, -0.0319, 0.1174, 0.2019, 0.0137, 0.01, 
            0.1595, 0.125, -0.0945, 0.1769, -0.0896, 0.1805, 0.0034, 0.0735
        ])

        HISTORICAL_INFLATION_UK_1928_2023 = np.array([
            -0.003, -0.009, -0.028, -0.043, -0.026, -0.021, 0.0, 0.007, 0.007, 0.034, 0.016, 0.028,
            0.168, 0.108, 0.071, 0.034, 0.027, 0.028, 0.031, 0.07, 0.077, 0.028, 0.031, 0.091, 0.092,
            0.031, 0.018, 0.045, 0.049, 0.037, 0.03, 0.006, 0.01, 0.034, 0.043, 0.02, 0.033, 0.048,
            0.039, 0.025, 0.047, 0.054, 0.064, 0.094, 0.071, 0.092, 0.16, 0.242, 0.165, 0.158, 0.083,
            0.134, 0.18, 0.119, 0.086, 0.046, 0.05, 0.061, 0.034, 0.042, 0.049, 0.078, 0.095, 0.059,
            0.037, 0.016, 0.024, 0.035, 0.024, 0.031, 0.034, 0.015, 0.03, 0.018, 0.017, 0.029, 0.03,
            0.028, 0.032, 0.043, 0.04, -0.005, 0.046, 0.052, 0.032, 0.03, 0.024, 0.01, 0.018, 0.036,
            0.033, 0.026, 0.015, 0.041, 0.116, 0.097
        ])

        HISTORICAL_CASH_UK_1928_2023 = np.array([
            0.045, 0.05, 0.03, 0.06, 0.02, 0.02, 0.02, 0.02, 0.02, 0.02,
            0.02, 0.02, 0.02, 0.02, 0.02, 0.02, 0.02, 0.02, 0.02, 0.02,
            0.02, 0.02, 0.02, 0.025, 0.04, 0.035, 0.03, 0.045, 0.055, 0.07,
            0.04, 0.04, 0.05, 0.06, 0.045, 0.04, 0.07, 0.06, 0.07, 0.08,
            0.07, 0.08, 0.07, 0.05, 0.09, 0.13, 0.115, 0.1125, 0.1425, 0.07,
            0.125, 0.17, 0.14, 0.1437, 0.1, 0.0906, 0.095, 0.1138, 0.1087, 0.0838,
            0.1288, 0.1487, 0.1388, 0.1037, 0.0688, 0.0537, 0.0612, 0.0638, 0.0594, 0.0725,
            0.0625, 0.055, 0.06, 0.04, 0.04, 0.0375, 0.0475, 0.045, 0.05, 0.055,
            0.02, 0.005, 0.005, 0.005, 0.005, 0.005, 0.005, 0.005, 0.0025, 0.005,
            0.0075, 0.0075, 0.001, 0.0025, 0.035, 0.0525,
        ])
        
        if sim_model == "Historical Rolling Sequence (US S&P 500 1928-2023)":
            hist_mean = np.prod(1 + HISTORICAL_RETURNS_SP500) ** (1 / len(HISTORICAL_RETURNS_SP500)) - 1
            median_returns = np.full(years, hist_mean)
        elif sim_model == "Historical Rolling Sequence (UK FTSE All-Share 1986-2023)":
            hist_mean = np.prod(1 + HISTORICAL_RETURNS_FTSE) ** (1 / len(HISTORICAL_RETURNS_FTSE)) - 1
            median_returns = np.full(years, hist_mean)
        else:
            median_returns = np.full(years, market_mean)
            
        df_median = simulate_scenario(inputs, median_returns)
        
        np.random.seed(42)
        success_count = 0
        final_pots = []
        
        for sim in range(sim_count):
            inf_returns = None
            cash_returns = None
            if sim_model == "Configurable Flat Return":
                returns = np.full(years, market_mean)
            elif sim_model == "Historical Rolling Sequence (US S&P 500 1928-2023)":
                start_idx = sim
                returns = np.array([HISTORICAL_RETURNS_SP500[(start_idx + i) % len(HISTORICAL_RETURNS_SP500)] for i in range(years)])
                if use_hist_inflation:
                    inf_returns = np.array([HISTORICAL_INFLATION_UK_1928_2023[(start_idx + i) % len(HISTORICAL_INFLATION_UK_1928_2023)] for i in range(years)])
                    cash_returns = np.array([HISTORICAL_CASH_UK_1928_2023[(start_idx + i) % len(HISTORICAL_CASH_UK_1928_2023)] for i in range(years)])
            elif sim_model == "Historical Rolling Sequence (UK FTSE All-Share 1986-2023)":
                start_idx = sim
                returns = np.array([HISTORICAL_RETURNS_FTSE[(start_idx + i) % len(HISTORICAL_RETURNS_FTSE)] for i in range(years)])
                if use_hist_inflation:
                    inf_start_idx = start_idx + 58
                    inf_returns = np.array([HISTORICAL_INFLATION_UK_1928_2023[(inf_start_idx + i) % len(HISTORICAL_INFLATION_UK_1928_2023)] for i in range(years)])
                    cash_returns = np.array([HISTORICAL_CASH_UK_1928_2023[(inf_start_idx + i) % len(HISTORICAL_CASH_UK_1928_2023)] for i in range(years)])
            else:
                returns = np.random.normal(market_mean, market_vol, years)
                
            df = simulate_scenario(inputs, returns, inf_returns, cash_returns)
            df['Simulation'] = sim
            all_results.append(df)
            
            final_pot = df['Total Pot'].iloc[-1]
            final_pots.append(final_pot)
            if final_pot > 0:
                success_count += 1
                
        df_all = pd.concat(all_results)
        failure_rate = ((sim_count - success_count) / sim_count) * 100
        
        final_pots_real = [df[df['Age'] == df['Age'].max()]['Total Pot Real'].iloc[0] for df in all_results]
        
        p10 = np.percentile(final_pots_real, 10)
        p50 = np.percentile(final_pots_real, 50)
        p90 = np.percentile(final_pots_real, 90)
        
        ages = df_all['Age'].unique()
        p10_series = df_all.groupby('Age')['Total Pot Real'].quantile(0.10).values
        p25_series = df_all.groupby('Age')['Total Pot Real'].quantile(0.25).values
        p50_series = df_all.groupby('Age')['Total Pot Real'].quantile(0.50).values
        p75_series = df_all.groupby('Age')['Total Pot Real'].quantile(0.75).values
        p90_series = df_all.groupby('Age')['Total Pot Real'].quantile(0.90).values
        
    st.subheader("Monte Carlo Simulation Results")
    
    failures_df = df_all[df_all['Total Pot'] < 1.0]
    earliest_fail_age = failures_df['Age'].min() if not failures_df.empty else None
    
    col1, col2, col3, col4 = st.columns(4)
    if earliest_fail_age is not None:
        col1.metric("Failure Rate (Pot depleted)", f"{failure_rate:.1f}%", f"Earliest: Age {earliest_fail_age}", delta_color="inverse")
    else:
        col1.metric("Failure Rate (Pot depleted)", f"{failure_rate:.1f}%", "100% Success Rate", delta_color="normal")
        
    col2.metric("Median Final Pot (Today's £)", f"£{p50:,.0f}")
    col3.metric("Pessimistic Final (10th %ile)", f"£{max(0, p10):,.0f}")
    col4.metric("Optimistic Final (90th %ile)", f"£{p90:,.0f}")
    
    st.write("👆 **Click on any line in the graph below** to load its exact year-by-year breakdown beneath it!")
    
    fig = go.Figure()
    
    # 10th-90th Percentile Band
    fig.add_trace(go.Scatter(
        x=ages, y=p10_series, mode='lines', line=dict(width=0), showlegend=False, hoverinfo="skip"
    ))
    fig.add_trace(go.Scatter(
        x=ages, y=p90_series, mode='lines', fill='tonexty', fillcolor='rgba(0, 200, 150, 0.15)', line=dict(width=0), name='10th - 90th Percentile (Likely)'
    ))
    
    # 25th-75th Percentile Band
    fig.add_trace(go.Scatter(
        x=ages, y=p25_series, mode='lines', line=dict(width=0), showlegend=False, hoverinfo="skip"
    ))
    fig.add_trace(go.Scatter(
        x=ages, y=p75_series, mode='lines', fill='tonexty', fillcolor='rgba(0, 200, 150, 0.3)', line=dict(width=0), name='25th - 75th Percentile (Most Likely)'
    ))
    
    # Median Line
    fig.add_trace(go.Scatter(
        x=ages, y=p50_series, mode='lines', line=dict(color='rgba(0, 150, 100, 1)', width=2, dash='dash'), name='Median Path'
    ))
    
    # FIX #7: Spread clickable paths evenly across ALL simulations, not just the first 50
    max_plotted = min(50, sim_count)
    if sim_count <= 50:
        plotted_sims = list(range(sim_count))
    else:
        step = sim_count / max_plotted
        plotted_sims = [int(i * step) for i in range(max_plotted)]
    
    for sim in plotted_sims:
        df_sim = df_all[df_all['Simulation'] == sim]
        if sim_model == "Historical Rolling Sequence (US S&P 500 1928-2023)":
            name = f"Retiring in {1928 + sim}"
        elif sim_model == "Historical Rolling Sequence (UK FTSE All-Share 1986-2023)":
            name = f"Retiring in {1986 + sim}"
        else:
            name = f"Simulation {sim}"
        fig.add_trace(go.Scatter(
            x=df_sim['Age'], y=df_sim['Total Pot Real'], mode='lines+markers', name=name, customdata=np.full(len(df_sim), sim),
            line=dict(color='rgba(0,0,255,0.05)' if sim_count > 50 else 'rgba(0,0,255,0.1)'), 
            marker=dict(size=12, opacity=0),
            showlegend=False
        ))
    
    # FIX #9: Use a distinct sentinel (-999) for the deterministic path so clicking it works
    DETERMINISTIC_SENTINEL = -999
    fig.add_trace(go.Scatter(
        x=df_median['Age'], y=df_median['Total Pot Real'], mode='lines+markers', name='Deterministic Path (Constant Return)',
        customdata=np.full(len(df_median), DETERMINISTIC_SENTINEL), line=dict(color='red', width=3),
        marker=dict(size=1, opacity=0)
    ))
    
    fig.add_hline(
        y=0, line_dash="dash", line_color="red", line_width=2,
        annotation_text="Failure Zone (Pot Depleted)", annotation_position="bottom right"
    )
    
    fig.update_layout(
        title="Monte Carlo Projected Wealth (in Today's Money)",
        xaxis_title="Age",
        yaxis_title="Total Portfolio Value (£ Real)",
        yaxis_tickformat='£,.0f', clickmode='event+select', hovermode='closest'
    )
    
    selection = st.plotly_chart(fig, use_container_width=True, on_select="rerun", selection_mode="points")
    
    sim_to_inspect = None
    if selection and "selection" in selection and "points" in selection["selection"]:
        points = selection["selection"]["points"]
        if len(points) > 0:
            custom_data = points[0].get("customdata")
            if custom_data is not None:
                sim_to_inspect = custom_data[0] if isinstance(custom_data, list) else custom_data
    
    st.subheader("Deep Dive: Selected Run Breakdown")
    
    # FIX #8 & #9: Handle deterministic sentinel, empty DataFrames, and no-click state
    if sim_to_inspect is not None and sim_to_inspect == DETERMINISTIC_SENTINEL:
        df_to_show = df_median
        st.write("Showing the **Deterministic Path** (constant expected return each year).")
    elif sim_to_inspect is not None and sim_to_inspect >= 0:
        # FIX #14: Specific exception types instead of bare except
        try:
            sim_id = int(sim_to_inspect)
            df_to_show = df_all[df_all['Simulation'] == sim_id].copy()
            # FIX #8: Guard against empty DataFrame
            if df_to_show.empty:
                df_to_show = df_median
                st.warning(f"Simulation {sim_id} is not in the plotted set. Showing deterministic path instead.")
            elif sim_model == "Historical Rolling Sequence (US S&P 500 1928-2023)":
                st.write(f"Showing exact year-by-year drawdown for a retirement starting in **{1928 + sim_id}**.")
            elif sim_model == "Historical Rolling Sequence (UK FTSE All-Share 1986-2023)":
                st.write(f"Showing exact year-by-year drawdown for a retirement starting in **{1986 + sim_id}**.")
            else:
                st.write(f"Showing exact year-by-year drawdown for **Monte Carlo Simulation {sim_id}**.")
        except (ValueError, KeyError, IndexError):
            df_to_show = df_median
            st.write("Showing the deterministic path.")
    else:
        df_to_show = df_median
        st.write("Showing the exact year-by-year drawdown logic applied using the constant expected return. Click a line above to inspect a specific simulation.")
    
    col1, col2 = st.columns([1, 1])
    
    with col1:
        fig_inc = go.Figure()
        fig_inc.add_trace(go.Scatter(x=df_to_show['Age'], y=df_to_show['Funded From: Guaranteed'], mode='lines', stackgroup='one', name='Guaranteed (DB + State)'))
        fig_inc.add_trace(go.Scatter(x=df_to_show['Age'], y=df_to_show['Funded From: Annuity'], mode='lines', stackgroup='one', name='Annuity Income'))
        fig_inc.add_trace(go.Scatter(x=df_to_show['Age'], y=df_to_show['Funded From: Savings'], mode='lines', stackgroup='one', name='Savings (Cash)'))
        fig_inc.add_trace(go.Scatter(x=df_to_show['Age'], y=df_to_show['Funded From: Cash ISA'], mode='lines', stackgroup='one', name='Cash ISA'))
        fig_inc.add_trace(go.Scatter(x=df_to_show['Age'], y=df_to_show['Funded From: S&S ISA'], mode='lines', stackgroup='one', name='S&S ISA'))
        fig_inc.add_trace(go.Scatter(x=df_to_show['Age'], y=df_to_show['Funded From: GIA'], mode='lines', stackgroup='one', name='GIA'))
        fig_inc.add_trace(go.Scatter(x=df_to_show['Age'], y=df_to_show['Funded From: 401k (Net)'], mode='lines', stackgroup='one', name='US 401k (Net)'))
        fig_inc.add_trace(go.Scatter(x=df_to_show['Age'], y=df_to_show['Funded From: DC Pension (Net)'], mode='lines', stackgroup='one', name='DC Pension (Net)'))
        fig_inc.add_trace(go.Scatter(x=df_to_show['Age'], y=df_to_show['Target Net Income'], mode='lines', name='Target Need', line=dict(color='red', dash='dash')))
        fig_inc.update_layout(title='Where is the Net Income Coming From?', xaxis_title='Age', yaxis_title='Income (£)', legend=dict(orientation="h", y=-0.2))
        st.plotly_chart(fig_inc, use_container_width=True)
    
    with col2:
        fig_ret = go.Figure()
        x_labels = df_to_show['Age']
        if sim_to_inspect is not None and sim_to_inspect >= 0:
            if sim_model == "Historical Rolling Sequence (US S&P 500 1928-2023)":
                start_year = 1928 + int(sim_to_inspect)
                x_labels = [f"Age {a} ({start_year + i})" for i, a in enumerate(df_to_show['Age'])]
            elif sim_model == "Historical Rolling Sequence (UK FTSE All-Share 1986-2023)":
                start_year = 1986 + int(sim_to_inspect)
                x_labels = [f"Age {a} ({start_year + i})" for i, a in enumerate(df_to_show['Age'])]
            
        annual_inflation = df_to_show['Inflation Factor'].pct_change().shift(-1).ffill()
        
        fig_ret.add_trace(go.Bar(
            x=x_labels, y=df_to_show['Stock Market Return'], name='Stock Market (Raw)', marker_color='rgba(150, 150, 150, 0.4)'
        ))
        fig_ret.add_trace(go.Bar(
            x=x_labels, y=df_to_show['Blended Portfolio Return'], name='Your Blended Portfolio', marker_color=['#00CC96' if r >= 0 else '#EF553B' for r in df_to_show['Blended Portfolio Return']]
        ))
        fig_ret.update_layout(
            title='Market Returns', xaxis_title='Age (and Year)', yaxis_title='Return Rate (%)',
            yaxis_tickformat='.1%', barmode='group', legend=dict(orientation="h", y=-0.2)
        )
        st.plotly_chart(fig_ret, use_container_width=True)
        
        fig_econ = go.Figure()
        fig_econ.add_trace(go.Scatter(
            x=x_labels, y=annual_inflation, mode='lines+markers', name='Inflation Rate', line=dict(color='rgba(255, 165, 0, 1)', width=3, dash='dot'), marker=dict(size=6)
        ))
        if 'Cash Yield' in df_to_show.columns:
            fig_econ.add_trace(go.Scatter(
                x=x_labels, y=df_to_show['Cash Yield'], mode='lines', name='BoE Base Rate (Cash Yield)', line=dict(color='blue', width=2)
            ))
        fig_econ.update_layout(
            title='Inflation & Interest Rates', xaxis_title='Age (and Year)', yaxis_title='Rate (%)',
            yaxis_tickformat='.1%', legend=dict(orientation="h", y=-0.2)
        )
        st.plotly_chart(fig_econ, use_container_width=True)
    
    st.write("---")
    
    fig_bal = go.Figure()
    
    fig_bal.add_trace(go.Scatter(x=df_to_show['Age'], y=df_to_show['Savings Balance'] / df_to_show['Inflation Factor'], mode='lines', stackgroup='one', name='Savings (Cash)'))
    fig_bal.add_trace(go.Scatter(x=df_to_show['Age'], y=df_to_show['Cash ISA Balance'] / df_to_show['Inflation Factor'], mode='lines', stackgroup='one', name='Cash ISA'))
    fig_bal.add_trace(go.Scatter(x=df_to_show['Age'], y=df_to_show['S&S ISA Balance'] / df_to_show['Inflation Factor'], mode='lines', stackgroup='one', name='S&S ISA'))
    fig_bal.add_trace(go.Scatter(x=df_to_show['Age'], y=df_to_show['GIA Balance'] / df_to_show['Inflation Factor'], mode='lines', stackgroup='one', name='GIA'))
    fig_bal.add_trace(go.Scatter(x=df_to_show['Age'], y=df_to_show['401k Balance (GBP)'] / df_to_show['Inflation Factor'], mode='lines', stackgroup='one', name='US 401k'))
    fig_bal.add_trace(go.Scatter(x=df_to_show['Age'], y=df_to_show['DC Pension Balance'] / df_to_show['Inflation Factor'], mode='lines', stackgroup='one', name='DC Pension'))
    fig_bal.update_layout(
        title="Remaining Pot Balances Over Time (in Today's Money)", xaxis_title='Age', yaxis_title='Balance (£ Real)',
        yaxis_tickformat='£,.0f', legend=dict(orientation="h", y=-0.2)
    )
    st.plotly_chart(fig_bal, use_container_width=True)
    
    format_dict = {col: "£{:,.0f}" for col in df_to_show.columns if col not in ['Age', 'Simulation', 'Stock Market Return', 'Blended Portfolio Return', 'Inflation Factor', 'Cash Yield']}
    format_dict['Stock Market Return'] = "{:.2%}"
    format_dict['Blended Portfolio Return'] = "{:.2%}"
    format_dict['Cash Yield'] = "{:.2%}"
    format_dict['Inflation Factor'] = "{:.2f}x"
    st.dataframe(df_to_show.drop(columns=['Simulation'], errors='ignore').style.format(format_dict))

with tab_help:
    st.header("How to use this Simulator")
    st.markdown('''
    Welcome to the Scottish Retirement Simulator. This tool mathematically models your exact lifetime tax burden (specifically for Scotland), sequence-of-returns risk, and drawdown strategy.

    ### 1. Personal Details
    * **Current Age** & **End Age**: Defines the total length of your retirement simulation.

    ### 2. Income Goals
    * **Target Net Income (Today's £)**: The spending money you want in your pocket *after* all taxes are paid. The simulator will automatically adjust your required withdrawals upwards every year to account for inflation. You can set different targets for different phases of retirement (e.g., active years vs later years).

    ### 3. Guaranteed Income
    * **UK State Pension**: Automatically taxed. You can set a custom **State Pension Inflation** rate (defaults to general inflation). In *Historical Mode*, this acts as a **floor** to simulate the Triple Lock (e.g. it rises with historical inflation, but never falls below your slider value).
    * **Defined Benefit Pension**: Automatically taxed. You can set a **DB Pension Inflation** rate. In *Historical Mode*, this acts as a **cap** (e.g. it rises with historical inflation, but never exceeds your slider value).
    * **DB Tax-Free Lump Sum**: Consumes your Lump Sum Allowance (LSA) and is paid tax-free at your DB start age.

    ### 4. Pensions & Allowances
    * **DC Pension & US 401(k)**: You can choose how you take tax-free cash from your DC pension (either taking 25% tax-free on every withdrawal via UFPLS, or sweeping £20k/yr into a S&S ISA via Phased PCLS).
    * **Lump Sum Allowance (LSA)**: Capped at £268,275 across your lifetime. Once exhausted, all pension withdrawals become 100% taxable.

    ### 5. Savings & Investments
    * **Cash Savings**: Earns interest at the Cash/Bond Yield rate. Interest is subject to the Personal Savings Allowance (PSA) and marginal income tax.
    * **Cash ISA & Stocks & Shares ISA**: 100% tax-free growth and withdrawals.
    * **General Investment Account (GIA)**: Capital gains are tracked exactly, with the £3,000 annual CGT exemption automatically applied before 20% higher-rate CGT.

    ### 6. Market Assumptions
    * **Simulation Models**: 
      - *Normal Distribution (Bell Curve)*: Monte Carlo simulations based on your input mean/volatility.
      - *Configurable Flat Return*: A fully deterministic, 0% volatility projection for baseline testing.
      - *Historical Rolling Sequence (S&P 500 or FTSE All-Share)*: Uses actual historical market returns to perfectly model Sequence of Returns Risk.
    * **Portfolio Equity Allocation**: For example, a 60% allocation means 60% is in Stocks (experiencing market returns), and 40% is in safe Cash/Bonds.
    * **Use Historical UK Inflation & Interest Rates**: (Visible for Historical models). Perfectly links UK historical inflation and the Bank of England Base Rate to the exact year of the simulation sequence.
    * **General Inflation Rate**: The flat assumed annual increase in the cost of living (if not using historical rates).
    * **Tax Bracket Inflation Match / Tax Bracket Inflation (%) (Fiscal Drag)**: 
      - *Historical Mode (Match %)*: How much the Scottish tax bands inflate relative to the historical inflation rate (e.g., 100% perfectly matches inflation; 50% means bands only rise by half the inflation amount).
      - *Standard Mode (Flat %)*: The fixed annual percentage by which tax bands increase. If this is lower than your General Inflation Rate, you will suffer "fiscal drag" (paying more tax over time as brackets stagnate). 
      - *Note*: The £100k Personal Allowance taper threshold is always frozen by law.
    * **Cash Yield**: The interest rate earned by your Cash Savings, Cash ISA, and the bond portion of your portfolio. (Hidden/dynamic if "Use Historical UK Inflation" is checked).
    * **Cash Buffer**: How many years of income to protect in cash during market downturns before being forced to sell equities.
    * **USD to GBP Rate**: Used to convert your US 401(k) balance.

    ### 7. Drawdown Strategy
    * **1. Tax-Optimised (Cap at Basic Rate, Protect Cash)** *(Recommended)*: Calculates headroom up to the Scottish Higher Rate threshold (£43,662). Draws taxable pensions up to this limit, then switches to tax-free ISAs/GIA to dodge the 42% bracket. Protects cash during market crashes (using Cash Buffer).
    * **2. Tax-Optimised (401k First)**: Same as #1, but explicitly targets the US 401(k) first to eliminate foreign tax reporting complexity.
    * **3. Dynamic Cash Buffer**: Sells equities in up-years, but burns cash in down-years to let stocks recover.
    * **4. Equities First**: Aggressively sells investments first, preserving cash indefinitely.
    * **5. Cash First**: Exhausts all cash before touching investments.

    ### 8. Reading the Graphs
    * **The Probability Cone**: The green shaded areas represent the statistical likelihood of your wealth. If the cone hits the red **Failure Zone**, there is a risk of running out of money.
    * **Clickable Paths**: Click any blue path in the top graph to load that exact simulation into the deep-dive charts below.
    * **Deep Dive Charts**: Shows exactly where your income came from each year, pot balances over time, and your Blended Portfolio Return vs the Raw Stock Market Return (including the exact historical Bank of England Cash Yields and Inflation).

    ### 9. Saving & Loading (Bookmarks)
    * **Generate Bookmark Link**: In the sidebar under the "💾 Save & Load Scenarios" section, click the "Generate Bookmark Link" button. This automatically saves all of your current settings directly into the web address (URL).
    * **Sharing**: You can copy this URL to send your exact retirement scenario to someone else.
    * **Loading**: Simply bookmark the URL in your browser. Whenever you visit that bookmark, the simulator will instantly load all of your custom settings.
    ''')

