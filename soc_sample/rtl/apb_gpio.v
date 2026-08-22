// apb_gpio.v — synthetic sample IP (APB GPIO controller), for
// coverage_agent's exclude-generation development/testing. This is
// NOT a real customer design — it exists to give the RTL scanner
// real, representative dead-code patterns to reason about:
//   - a hard tie-off (pslverr, always 0 by design)
//   - a reserved/tied bit range (status_reg upper bits, width set by
//     the NUM_GPIO parameter)
//   - a parameter-gated block (interrupt logic, removed entirely
//     when ENABLE_IRQ==0 — some derivatives don't wire an IRQ line)
//   - one genuinely live, untested bit (gpio_out[3]) with no RTL
//     justification, to prove the tool won't fabricate an excuse

module apb_gpio #(
    parameter NUM_GPIO   = 8,
    parameter ENABLE_IRQ = 1
) (
    input  wire        pclk,
    input  wire        presetn,
    input  wire        psel,
    input  wire        penable,
    input  wire        pwrite,
    input  wire [7:0]  paddr,
    input  wire [31:0] pwdata,
    output reg  [31:0] prdata,
    output wire        pready,
    output wire        pslverr,
    inout  wire [NUM_GPIO-1:0] gpio,
    output wire                irq
);

  reg  [31:0] gpio_dir;
  reg  [31:0] gpio_out;
  reg  [NUM_GPIO-1:0] gpio_in_sync;
  wire [31:0] status_reg;

  // -----------------------------------------------------------------
  // Single-cycle APB, no illegal-address trap implemented: this
  // peripheral never signals a slave error. Tied off by design.
  // -----------------------------------------------------------------
  assign pslverr = 1'b0;
  assign pready  = 1'b1;

  // Status register: bits above NUM_GPIO-1 are reserved and tied to 0
  // in every known instantiation (NUM_GPIO < 32 always).
  assign status_reg[NUM_GPIO-1:0] = gpio_in_sync;
  assign status_reg[31:NUM_GPIO]  = {(32 - NUM_GPIO){1'b0}};

  always @(posedge pclk or negedge presetn) begin
    if (!presetn)
      gpio_in_sync <= {NUM_GPIO{1'b0}};
    else
      gpio_in_sync <= gpio;
  end

  always @(posedge pclk or negedge presetn) begin
    if (!presetn) begin
      gpio_dir <= 32'h0;
      gpio_out <= 32'h0;
    end else if (psel && penable && pwrite) begin
      case (paddr[7:2])
        6'h0: gpio_dir <= pwdata;
        6'h1: gpio_out <= pwdata;
        default: ;
      endcase
    end
  end

  always @(*) begin
    case (paddr[7:2])
      6'h0: prdata = gpio_dir;
      6'h1: prdata = gpio_out;
      6'h2: prdata = status_reg;
      default: prdata = 32'h0;
    endcase
  end

  assign gpio = gpio_dir[NUM_GPIO-1:0] ? gpio_out[NUM_GPIO-1:0] : {NUM_GPIO{1'bz}};

  // -----------------------------------------------------------------
  // Interrupt logic is entirely absent when ENABLE_IRQ==0 — some
  // derivatives don't wire an IRQ line at the SoC level, so this
  // whole block is dead in that configuration.
  // -----------------------------------------------------------------
  generate
    if (ENABLE_IRQ) begin : g_irq_enabled
      reg irq_pending;
      always @(posedge pclk or negedge presetn) begin
        if (!presetn)
          irq_pending <= 1'b0;
        else if (|(gpio_in_sync & ~gpio_dir[NUM_GPIO-1:0]))
          irq_pending <= 1'b1;
        else if (psel && penable && pwrite && paddr[7:2] == 6'h3)
          irq_pending <= 1'b0;
      end
      assign irq = irq_pending;
    end else begin : g_irq_disabled
      assign irq = 1'b0;
    end
  endgenerate

endmodule
