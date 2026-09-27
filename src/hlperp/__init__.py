"""AI-driven perpetual futures trading bot for Hyperliquid.

Reference architecture: one decision per market tick, a typed model call, a risk
engine between the model and the exchange, and honest P&L that includes both
fees and funding. Paper mode is the default; live trading is gated behind an
explicit interlock.
"""

__version__ = "0.1.0"
