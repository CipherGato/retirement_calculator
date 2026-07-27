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

# --- Import & Export (Must run before widgets are instantiated) ---
with st.sidebar.expander("8. Save & Load Scenarios", expanded=False):
    st.markdown("**Tip:** You can generate a URL to save or share your exact scenario.")
    if st.button("🔗 Generate Bookmark Link", help="Updates the URL with your current settings so you can bookmark or share it."):
        b64_str = base64.urlsafe_b64encode(json.dumps(st.session_state.cfg).encode("utf-8")).decode("utf-8").rstrip("=")
        st.query_params["state"] = b64_str
        st.success("URL updated! You can now bookmark or copy the URL.")
    
    st.write("---")
    st.write("Or, export/import to a file:")
    json_to_export = json.dumps(st.session_state.cfg, indent=2)
    
    st.download_button(
        label="📥 Export Settings (JSON)",
        data=json_to_export,
        file_name="retirement_settings.json",
        mime="application/json"
    )
    
    uploaded_file = st.file_uploader("📤 Import Settings (JSON)", type=["json"])
    if True:
        if st.button("Apply Imported Settings"):
            try:
                # Seek to 0 in case it was read before
                pass
                imported_cfg = {"current_age": 70, "end_age": 95, "spending_points": [{"From Age": 70, "Target Net (\xc2\xa3)": 60000}], "inflation_rate_pct": 3.0, "tax_band_inflation_pct": 1.0, "usd_gbp_rate": 0.8, "state_pension": 15000.0, "state_pension_age": 68, "db_pension": 5000, "db_age": 70, "state_pension_inflation_pct": 3.0, "db_pension_inflation_pct": 2.0, "savings_start": 50000, "cash_isa_start": 20000, "ss_isa_start": 100000, "gia_start": 10000, "dc_start": 200000, "k401_start_usd": 50000, "k401_access_age": 59.5, "sim_model_index": 0, "equity_allocation_pct": 80.0, "market_mean_pct": 6.0, "market_vol_pct": 10.0, "cash_interest_pct": 4.0, "sim_count": 100, "drawdown_strategy_index": 2, "dc_tax_free_index": 0, "lsa_start": 200000, "cash_buffer_years": 3}
                st.session_state.cfg = imported_cfg
                
                # Inject imported values directly into session state to force Streamlit to adopt them
                for k, v in imported_cfg.items():
                    widget_key = f"{k}_widget"
                    if k == "drawdown_strategy_index":
                        st.session_state["drawdown_strategy_widget"] = ["Equities First", "Equities First (401k First)", "Tax-Optimised (Cap at Basic Rate, Protect Cash)", "Tax-Optimised (401k First, Cap at Basic Rate, Protect Cash)"][min(v, 3)]
                    elif k == "sim_model_index":
                        st.session_state["sim_model_widget"] = ["Normal Distribution (Bell Curve)", "Historical Rolling Sequence (US S&P 500 1928-2023)", "Historical Rolling Sequence (UK FTSE All-Share 1986-2023)"][min(v, 2)]
                    elif k == "dc_tax_free_index":
                        st.session_state["dc_tax_free_widget"] = ["100% Taxable (Already taken / Default)", "UFPLS (25% Tax-Free per withdrawal)", "Phased PCLS (Move £20k/yr Tax-Free to S&S ISA)"][min(v, 2)]
                    elif k == "spending_points":
                        # data_editor does not allow programmatic state assignment. We must delete its key to force it to use get_val().
                        if widget_key in st.session_state:
                            del st.session_state[widget_key]
                    else:
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
def simulate_scenario(inputs, market_returns):
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
    
    for i in range(years):
        inflation_factor = (1 + inputs['inflation_rate']) ** i
        tax_inflation_factor = (1 + inputs['tax_band_inflation']) ** i
        
        spending_points = inputs.get('spending_points', [])
        base_target_net = 50000 # Fallback
        if spending_points:
            sorted_pts = sorted(spending_points, key=lambda x: x.get('From Age', 0), reverse=True)
            for pt in sorted_pts:
                if age >= pt.get('From Age', 0):
                    base_target_net = pt.get('Target Net (£)', 0)
                    break
            # If age is before all points, use the first point chronologically (which is the last in reverse sort)
            if base_target_net == 0 and sorted_pts:
                base_target_net = sorted_pts[-1].get('Target Net (£)', 0)

        target_net = base_target_net * inflation_factor
        
        db_inflation_factor = (1 + inputs.get('db_inflation', inputs['inflation_rate'])) ** i
        sp_inflation_factor = (1 + inputs.get('sp_inflation', inputs['inflation_rate'])) ** i
        db_income = inputs['db_pension'] * db_inflation_factor if age >= inputs['db_age'] else 0
        state_pension = inputs['state_pension'] * sp_inflation_factor if age >= inputs['state_pension_age'] else 0
        
        guaranteed_gross = db_income + state_pension
        guaranteed_net = guaranteed_gross - calculate_scottish_tax(guaranteed_gross, tax_inflation_factor)
        
        shortfall_net = max(0, target_net - guaranteed_net)
        
        current_gross = guaranteed_gross
        
        withdrawals = {
            'Savings': 0, 'Cash_ISA': 0, 'SS_ISA': 0, 'GIA': 0, 'DC_Pension_Net': 0, 'US_401k_Net': 0
        }
        
        k401_available = age >= inputs['k401_access_age']
        
        ret = market_returns[i]
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
            else: # "Cash First (Burn Cash immediately)"
                draw_order = [
                    ('Savings', 'tax_free'), ('Cash_ISA', 'tax_free'), ('US_401k', 'taxable'),
                    ('SS_ISA', 'tax_free'), ('GIA', 'gia'), ('DC_Pension', 'taxable')
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
            'Funded From: Guaranteed': guaranteed_net,
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
            'Total Pot': total_pot
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
        
    with st.expander("2. Income Goals & Economy", expanded=False):
        st.write("Target Net Income (Today's £) by Age")
        st.caption("Define your spending phases. The simulator automatically inflates these targets over time. Add as many phases as you like.")
        default_spending = [{"From Age": 55, "Target Net (£)": 50000}, {"From Age": 75, "Target Net (£)": 35000}]
        spending_points = st.data_editor(get_val('spending_points', default_spending), num_rows="dynamic", key="spending_points_widget", use_container_width=True, hide_index=True)
        
        if spending_points:
            try:
                first_target = float(spending_points[0].get("Target Net (£)", 0))
                gross_first = gross_up_for_tax(first_target, 0, 1.0)
                st.markdown(f"<p style='margin-top:-5px; margin-bottom:15px; font-size:14px; color:gray;'>First phase equivalent to Gross Salary: <b>£{gross_first:,.0f}</b> <br/><i>(Note: Retirees do not pay National Insurance on pension income)</i></p>", unsafe_allow_html=True)
            except Exception:
                pass
        
        st.write("---")
        inflation_rate_pct = st.slider("General Inflation Rate (%)", 0.0, 10.0, get_val('inflation_rate_pct', 2.5), 0.1, key="inflation_rate_pct_widget", help="Assumed annual increase in the cost of goods and services.")
        inflation_rate = inflation_rate_pct / 100.0
        
        tax_band_inflation_pct = st.slider("Tax Bracket Inflation (%) (Fiscal Drag)", 0.0, 10.0, get_val('tax_band_inflation_pct', 0.0), 0.1, key="tax_band_inflation_pct_widget", help="Assumed annual increase in Scottish tax thresholds. If this is lower than inflation, you will suffer 'fiscal drag' and pay more tax over time. Note: the £100k PA taper threshold is always frozen (as in reality).")
        tax_band_inflation = tax_band_inflation_pct / 100.0
        
        usd_gbp_rate = st.number_input("USD to GBP Exchange Rate", min_value=0.01, max_value=5.0, value=get_val('usd_gbp_rate', 0.75), step=0.01, key="usd_gbp_rate_widget", help="Exchange rate for converting your US 401k to GBP.")
        
    with st.expander("3. Guaranteed Income", expanded=False):
        state_pension = st.number_input("UK State Pension (Annual £)", min_value=0.0, value=float(get_val('state_pension', 12547.60)), step=100.0, key="state_pension_widget", help="Current value of the full UK State Pension.")
        state_pension_age = st.number_input("State Pension Age", min_value=55, max_value=80, value=get_val('state_pension_age', 67), key="state_pension_age_widget", help="The age you will start receiving the UK State Pension.")
        db_pension = st.number_input("Defined Benefit Pension (Annual £)", min_value=0, value=get_val('db_pension', 0), key="db_pension_widget", help="Annual gross income from your Defined Benefit (final salary) pension.")
        db_age = st.number_input("DB Pension Start Age", min_value=50, max_value=80, value=get_val('db_age', 65), key="db_age_widget", help="The age your DB pension begins paying out.")
        
        st.write("---")
        state_pension_inflation_pct = st.slider("State Pension Inflation (%)", 0.0, 10.0, get_val('state_pension_inflation_pct', inflation_rate_pct), 0.1, key="state_pension_inflation_pct_widget", help="Annual increase in the UK State Pension. The Triple Lock guarantees the higher of inflation, average earnings growth, or 2.5%. Defaults to general inflation (conservative).")
        db_pension_inflation_pct = st.slider("DB Pension Inflation (%)", 0.0, 10.0, get_val('db_pension_inflation_pct', min(inflation_rate_pct, 2.5)), 0.1, key="db_pension_inflation_pct_widget", help="Annual increase in your Defined Benefit pension. Many schemes cap increases at CPI or 2.5%, whichever is lower. Defaults to min(inflation, 2.5%).")
        
    with st.expander("4. Pensions & Allowances", expanded=False):
        dc_start = st.number_input("Defined Contribution Pension (£)", min_value=0, value=get_val('dc_start', 0), step=10000, key="dc_start_widget", help="Defined Contribution Pension. Handled according to the strategy chosen below.")
        
        dc_options = [
            "100% Taxable (Already taken / Default)",
            "25% Tax-Free (UFPLS) on every withdrawal",
            "Phased PCLS (Move £20k/yr Tax-Free to S&S ISA)"
        ]
        saved_dc_index = get_val('dc_tax_free_index', 0)
        if saved_dc_index >= len(dc_options): saved_dc_index = 0
        
        dc_tax_free_method = st.selectbox("DC Pension Tax-Free Handling", dc_options, index=saved_dc_index, key="dc_tax_free_widget", help="UFPLS makes every withdrawal 25% tax-free. Phased PCLS transfers £20k a year into your ISA until the allowance runs out. 100% Taxable assumes you've already taken the cash.")
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
        sim_models = ["Normal Distribution (Bell Curve)", "Historical Rolling Sequence (US S&P 500 1928-2023)", "Historical Rolling Sequence (UK FTSE All-Share 1986-2023)"]
        sim_model_index = get_val('sim_model_index', 0)
        if sim_model_index >= len(sim_models): sim_model_index = 0
        sim_model = st.selectbox("Simulation Model", sim_models, index=sim_model_index, key="sim_model_widget", help="Bell Curve uses random math. Historical Rolling uses real sequence returns.")
        
        equity_allocation_pct = st.slider("Portfolio Equity Allocation (Stocks vs Bonds/Cash)", 0.0, 100.0, get_val('equity_allocation_pct', 100.0), 5.0, key="equity_allocation_pct_widget", help="100% means fully invested in stocks. 60% means 60% stocks and 40% bonds/cash.")
        
        market_mean_pct = get_val('market_mean_pct', 5.0)
        market_vol_pct = get_val('market_vol_pct', 12.0)
        
        if sim_model == "Normal Distribution (Bell Curve)":
            market_mean_pct = st.slider("Expected Stock Market Return (%)", 0.0, 15.0, market_mean_pct, 0.1, key="market_mean_pct_widget", help="Expected average annual return of the stock market.")
            market_vol_pct = st.slider("Stock Market Volatility (%)", 0.0, 30.0, market_vol_pct, 0.5, key="market_vol_pct_widget", help="Expected volatility (standard deviation) of the stock market.")
            sim_count = st.number_input("Monte Carlo Simulations", min_value=1, max_value=1000, value=get_val('sim_count', 500), key="sim_count_widget", help="Number of random lifetimes to simulate.")
        elif sim_model == "Historical Rolling Sequence (US S&P 500 1928-2023)":
            st.info("Using exactly 96 retirements, each starting in a different historical year (1928 to 2023). Stock Return sliders disabled.")
            sim_count = 96
        else:
            st.info("Using exactly 38 retirements, each starting in a different historical year (1986 to 2023). Stock Return sliders disabled.")
            sim_count = 38
            
        market_mean = market_mean_pct / 100.0
        market_vol = market_vol_pct / 100.0
        
        cash_interest_pct = st.slider("Cash / Bond Yield Rate (%)", 0.0, 10.0, get_val('cash_interest_pct', 3.0), 0.1, key="cash_interest_pct_widget", help="Yield rate applied to Cash Savings, Cash ISAs, and the non-equity portion of your investment portfolio.")
        cash_interest = cash_interest_pct / 100.0
        
    with st.expander("7. Drawdown Strategy", expanded=True):
        drawdown_options = [
            "Tax-Optimised (Cap at Basic Rate, Protect Cash)",
            "Tax-Optimised (401k First, Cap at Basic Rate, Protect Cash)",
            "Dynamic Cash Buffer (Equities in Up years, Cash in Down years)",
            "Equities First (Preserve Cash completely)",
            "Cash First (Burn Cash immediately)"
        ]
        saved_index = get_val('drawdown_strategy_index', 0)
        if saved_index >= len(drawdown_options): saved_index = 0
        
        drawdown_strategy = st.selectbox("Drawdown Strategy", drawdown_options, index=saved_index, key="drawdown_strategy_widget", help="Tax-Optimised mathematically blends pots to avoid the 42% tax bracket. Other options rigidly follow a sequence.")
        
        cash_buffer_years = get_val('cash_buffer_years', 2)
        if drawdown_strategy in ["Tax-Optimised (Cap at Basic Rate, Protect Cash)", "Tax-Optimised (401k First, Cap at Basic Rate, Protect Cash)"]:
            cash_buffer_years = st.slider("Cash Buffer (Years of Income)", 0, 10, cash_buffer_years, 1, key="cash_buffer_years_widget", help="How many years of income to protect in Cash Savings / Cash ISA before being forced into the 42% tax bracket.")

new_cfg = {
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
    'state_pension_inflation_pct': state_pension_inflation_pct,
    'db_pension_inflation_pct': db_pension_inflation_pct,
    'savings_start': savings_start,
    'cash_isa_start': cash_isa_start,
    'ss_isa_start': ss_isa_start,
    'gia_start': gia_start,
    'dc_start': dc_start,
    'k401_start_usd': k401_start_usd,
    'k401_access_age': k401_access_age,
    'sim_model_index': sim_models.index(sim_model),
    'equity_allocation_pct': equity_allocation_pct,
    'market_mean_pct': market_mean_pct,
    'market_vol_pct': market_vol_pct,
    'cash_interest_pct': cash_interest_pct,
    'sim_count': sim_count,
    'drawdown_strategy_index': drawdown_options.index(drawdown_strategy),
    'dc_tax_free_index': dc_options.index(dc_tax_free_method),
    'lsa_start': lsa_start,
    'cash_buffer_years': cash_buffer_years
}

# Save to internal session state
import time
with open("debug_log.txt", "a") as f: f.write(f"\n--- RUN {time.time()} ---\nCFG: {st.session_state.cfg}\nNEW: {new_cfg}\n")
if new_cfg != st.session_state.cfg:
    st.session_state.cfg = new_cfg

