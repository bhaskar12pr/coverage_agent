// apb_gpio_v2.v — synthetic sample IP, v2. Same interface as
// apb_gpio.v (v1) except: v2 adds an illegal-address trap, so
// pslverr is now REAL logic, not a tie-off. This exists to prove
// exclude-generation must re-scan RTL per IP version rather than
// reusing exclusions learned from a different version of the IP —
// a v1 "pslverr is tied off" exclusion would be WRONG here.

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
    output reg         pslverr,
    inout  wire [NUM_GPIO-1:0] gpio,
    output wire                irq
);

  reg  [31:0] gpio_dir;
  reg  [31:0] gpio_out;
  reg  [NUM_GPIO-1:0] gpio_in_sync;
  wire [31:0] status_reg;

  assign pready = 1'b1;

  // v2: illegal address (above the 3 valid registers) now raises
  // pslverr instead of silently reading/writing zero.
  always @(*) begin
    pslverr = psel && penable && (paddr[7:2] > 6'h2);
  end

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
