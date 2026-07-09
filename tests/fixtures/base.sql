SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS=0;

DROP TABLE IF EXISTS `users`;
CREATE TABLE `users` (
  `id` int(11) NOT NULL,
  `name` varchar(100) NOT NULL,
  `email` varchar(200) DEFAULT NULL,
  `updated_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
INSERT INTO `users` VALUES
(1,'Alice','alice@ex.com','2024-01-01 00:00:00'),
(2,'Bob','bob@ex.com','2024-01-01 00:00:00'),
(3,'Carol','carol@ex.com','2024-01-01 00:00:00'),
(4,'Dave','dave@ex.com','2024-01-01 00:00:00');

DROP TABLE IF EXISTS `orders`;
CREATE TABLE `orders` (
  `order_id` int(11) NOT NULL,
  `line_no` int(11) NOT NULL,
  `sku` varchar(32) NOT NULL,
  `qty` int(11) NOT NULL,
  PRIMARY KEY (`order_id`,`line_no`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
INSERT INTO `orders` VALUES
(100,1,'A',5),
(100,2,'B',3),
(101,1,'C',1);

DROP TABLE IF EXISTS `tags`;
CREATE TABLE `tags` (
  `name` varchar(50) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
INSERT INTO `tags` VALUES ('red'),('blue');
